"""
probe.py — pure logic for the Active Prober (delivery-quality telemetry).

Everything here is PURE (no I/O) so it unit-tests against captured payloads,
mirroring gateway/radar.py's design. The I/O lives in agents/prober/run.py.

Pipeline (see PROBER_SPEC, 2026-07-07):
    SELECT   select_candidates(ranked, recent) → T0 set + capped T1 (paid) set
    T0       t0_checks(status, body, catalog_price) → alive / wellformed /
             price_matches / mpp_option  (free, all candidates)
    T1       t1_evaluate(data, out_schema) + the runner's settle outcome →
             settle_ok / http_ok / latency_ms / response_nonempty / schema_ok /
             paid_but_no_data  (paid, selected candidates)
    SCORE    score(probes) → per-service delivery_rate, delivery_factor, flags
             (consumed by gateway/radar.py::decide() — AGE-7)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional
from urllib.parse import urlsplit

# Canonical needs list (env PROBER_NEEDS overrides in the runner).
DEFAULT_NEEDS = [
    "web search", "token price", "twitter data", "llm inference",
    "wallet screening", "news", "market data", "pdf ocr",
]

TOP_K_PER_NEED = 3          # survivors taken per need from rank()
DEFAULT_MAX_PAID = 15       # PROBER_MAX_PAID_PROBES default
WINDOW_DAYS = 30            # scoring window

# delivery_factor thresholds (PROBER_SPEC "Scoring model")
FACTOR_UNPROBED = 1.0       # neutral — never punish absence of data
FACTOR_GOOD = 1.15          # rate >= 0.9 — modest boost; usage still dominates
FACTOR_BAD = 0.25           # rate < 0.5 — heavy downrank
GOOD_RATE = 0.9
BAD_RATE = 0.5

FLAG_NO_DELIVERY = "took_payment_no_delivery"


# ── SELECT ─────────────────────────────────────────────────────────────────────

def _dedup_key(cand: dict) -> tuple[str, str]:
    """Dedup by (host, payTo) per spec — one probe per service, not per listing."""
    host = urlsplit(cand.get("url") or "").netloc.lower()
    return (host, (cand.get("pay_to") or "").lower())


def select_candidates(
    ranked: dict[str, list[dict]],
    recent: Iterable[dict] = (),
    max_paid: int = DEFAULT_MAX_PAID,
    top_k: int = TOP_K_PER_NEED,
) -> dict[str, list[dict]]:
    """Budget-bounded, deterministic candidate selection. PURE.

    `ranked` maps need → rank()['results'] (already junk-filtered survivors,
    quality-sorted). `recent` is any service verified_route recommended in the
    last 7 days (freshness guarantee — never recommend something unprobed).

    Returns {"t1": capped paid set, "t0": full deduped set} — T0 free probes
    run on everything regardless (they cost nothing).
    """
    paid: list[dict] = []
    t0: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(cand: dict, need: Optional[str]) -> None:
        if not cand or not cand.get("url"):
            return
        key = _dedup_key(cand)
        if key in seen:
            return
        seen.add(key)
        cand = {**cand, "need": need}   # human category for scores/leaderboard
        t0.append(cand)
        if len(paid) < max_paid:
            paid.append(cand)

    # Recent recommendations FIRST — the freshness guarantee outranks sweep order.
    for cand in recent:
        add(cand, need="recently recommended")
    for need in sorted(ranked):
        for cand in ranked[need][:top_k]:
            add(cand, need=need)

    return {"t1": paid, "t0": t0}


# ── T0 (free probes) ───────────────────────────────────────────────────────────

def _parse_accepts(body: dict | None) -> list[dict]:
    """All advertised payment options: x402 `accepts[]` + AgentPay-style
    JSON-body `payment_options` (where Stellar/MPP options live off-`accepts`)."""
    if not isinstance(body, dict):
        return []
    opts = list(body.get("accepts") or [])
    extra = body.get("payment_options")
    if isinstance(extra, list):
        opts.extend(o for o in extra if isinstance(o, dict))
    elif isinstance(extra, dict):
        opts.extend(o for o in extra.values() if isinstance(o, dict))
    return [o for o in opts if isinstance(o, dict)]


def _option_amount_usd(opt: dict) -> Optional[Decimal]:
    """USDC amount of one option, atomic (6dp) or decimal string. None = unusable."""
    raw = opt.get("amount", opt.get("maxAmountRequired"))
    if raw is None:
        return None
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if d <= 0:
        return None
    # Atomic USDC (integers >= 100 == $0.0001+) vs already-decimal ("0.01").
    return d / Decimal(1_000_000) if d == d.to_integral_value() and d >= 100 else d


def _option_sane(opt: dict) -> bool:
    """amount parses positive, and the option names a network or asset."""
    if _option_amount_usd(opt) is None:
        return False
    return bool(opt.get("network") or opt.get("asset") or opt.get("chain"))


_MPP_MARKERS = ("mpp", "tempo")
_USDG_MARKERS = ("usdg", "eip155:46630", "robinhood")


def _option_hay(opt: dict) -> str:
    return " ".join(
        str(opt.get(k, "")) for k in ("network", "scheme", "rail", "chain", "asset", "protocol")
    ).lower()


def _is_mpp_option(opt: dict) -> bool:
    """[MR-3] Does this option advertise MPP/Tempo? Detection only, never settled."""
    return any(m in _option_hay(opt) for m in _MPP_MARKERS)


def _is_usdg_option(opt: dict) -> bool:
    """AGE-18: USDG / Robinhood Chain (eip155:46630) option. Detection only,
    never settled — same rail-agnostic stance as the MPP label."""
    return any(m in _option_hay(opt) for m in _USDG_MARKERS)


def _decode_payment_required(headers: dict | None) -> dict | None:
    """x402 v2 PAYMENT-REQUIRED / X-PAYMENT-REQUIRED header → payload dict.
    Base64 JSON per spec (raw JSON tolerated). None when absent/undecodable.
    Many sellers send the requirements ONLY here, with an empty body — the
    first live sweep (2026-07-10) found 10/15 such 402s."""
    if not headers:
        return None
    import base64
    lowered = {str(k).lower(): v for k, v in headers.items()}
    raw = lowered.get("payment-required") or lowered.get("x-payment-required")
    if not raw:
        return None
    for decode in (
        lambda s: json.loads(base64.b64decode(s + "=" * (-len(s) % 4))),
        json.loads,
    ):
        try:
            payload = decode(raw)
            return payload if isinstance(payload, dict) else None
        except Exception:
            continue
    return None


def t0_checks(status_code: Optional[int], body: dict | str | None,
              catalog_price_usd: Decimal | str | None = None,
              headers: dict | None = None) -> dict:
    """Evaluate one free probe. PURE.

    alive          — endpoint responded at all (any HTTP status)
    x402_wellformed— 402 returned AND at least one sane payment option parses
                     (from the body OR the PAYMENT-REQUIRED header)
    price_matches  — 402 amount == catalog amount (price honesty!); None when
                     either side is unknown (never penalize missing data)
    mpp_option     — [MR-3] an MPP/Tempo option is advertised (label only)
    """
    alive = status_code is not None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            body = None
    opts = _parse_accepts(body) if status_code == 402 else []
    if status_code == 402:
        opts.extend(_parse_accepts(_decode_payment_required(headers)))
    sane = [o for o in opts if _option_sane(o)]
    wellformed = status_code == 402 and bool(sane)

    price_matches: Optional[bool] = None
    if wellformed and catalog_price_usd is not None:
        try:
            catalog = Decimal(str(catalog_price_usd))
            amounts = [a for a in (_option_amount_usd(o) for o in sane) if a is not None]
            if amounts:
                price_matches = any(a == catalog for a in amounts)
        except (InvalidOperation, ValueError):
            price_matches = None

    return {
        "alive": alive,
        "x402_wellformed": wellformed,
        "price_matches": price_matches,
        "mpp_option": any(_is_mpp_option(o) for o in opts),
        "usdg_option": any(_is_usdg_option(o) for o in opts),
    }


# ── T1 (paid probes) ───────────────────────────────────────────────────────────

def t1_evaluate(data: object, out_schema: dict | None = None,
                min_length: int = 2) -> dict:
    """Judge a paid response body. PURE. The settle/http outcome comes from the
    runner (it owns the SDK call); this judges only what came back.

    response_nonempty — parses as JSON/text and is longer than a threshold
    schema_ok         — when a schema was advertised: >= 50% of its top-level
                        keys present (soft match). No schema → None (skip,
                        don't penalize).
    """
    if isinstance(data, (dict, list)):
        nonempty = len(data) >= 1 and bool(json.dumps(data, default=str) != "{}")
    elif isinstance(data, str):
        nonempty = len(data.strip()) >= min_length
    else:
        nonempty = data is not None

    schema_ok: Optional[bool] = None
    keys = _schema_keys(out_schema)
    if keys:
        if isinstance(data, dict):
            present = sum(1 for k in keys if k in data)
            schema_ok = (present / len(keys)) >= 0.5
        else:
            schema_ok = False

    return {"response_nonempty": nonempty, "schema_ok": schema_ok}


def _schema_keys(out_schema: dict | None) -> list[str]:
    """Advertised top-level keys from an outputSchema-ish dict. Tolerant of the
    common shapes: JSON-schema {properties: {...}}, bazaar info.output {...},
    or a plain example object."""
    if not isinstance(out_schema, dict) or not out_schema:
        return []
    props = out_schema.get("properties")
    if isinstance(props, dict) and props:
        return list(props)
    inner = out_schema.get("output")
    if isinstance(inner, dict) and inner:
        return _schema_keys(inner) or list(inner)
    reserved = {"type", "description", "required", "title", "$schema"}
    return [k for k in out_schema if k not in reserved]


def paid_but_no_data(settle_ok: bool, http_ok: bool) -> bool:
    """The worst flag: took money, gave nothing."""
    return bool(settle_ok and not http_ok)


# ── SCORE ──────────────────────────────────────────────────────────────────────

def delivery_factor(rate: Optional[float], probed: bool) -> float:
    """PROBER_SPEC scoring model. PURE.

        unprobed            → 1.00  (neutral)
        probed, rate ≥ 0.9  → 1.15
        probed, 0.5–0.9     → 1.00 − 0.5×(0.9 − rate)
        probed, < 0.5       → 0.25
    """
    if not probed or rate is None:
        return FACTOR_UNPROBED
    if rate >= GOOD_RATE:
        return FACTOR_GOOD
    if rate >= BAD_RATE:
        return round(1.0 - 0.5 * (GOOD_RATE - rate), 4)
    return FACTOR_BAD


def _delivered(p: dict) -> bool:
    """One paid probe counts as delivered iff settle ∧ http ∧ nonempty ∧ schema_ok?
    (schema_ok None = not advertised = not counted against)."""
    if not (p.get("settle_ok") and p.get("http_ok") and p.get("response_nonempty")):
        return False
    return p.get("schema_ok") is not False


def score(probes: Iterable[dict], window_days: int = WINDOW_DAYS,
          now: Optional[datetime] = None) -> list[dict]:
    """Aggregate raw probe rows → one service_scores row per resource_url. PURE.

    Each probe row: {resource_url, probed_at (iso), probe_type, settle_ok,
    http_ok, response_nonempty, schema_ok, latency_ms, ...}. Only paid probes
    inside the window feed delivery_rate; any paid_but_no_data in the window
    adds FLAG_NO_DELIVERY (hard-drop from recommendation downstream).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    by_url: dict[str, list[dict]] = {}
    for p in probes:
        url = p.get("resource_url")
        if not url:
            continue
        ts = _parse_ts(p.get("probed_at"))
        if ts is not None and ts < cutoff:
            continue
        by_url.setdefault(url, []).append(p)

    rows: list[dict] = []
    for url, group in sorted(by_url.items()):
        # skipped = unscoreable (cap reached, buyer-side params/method
        # rejection before any payment) — raw evidence only, never delivery.
        paid = [p for p in group
                if p.get("probe_type") == "paid" and not p.get("skipped")]
        n = len(paid)
        delivered = sum(1 for p in paid if _delivered(p))
        rate = (delivered / n) if n else None
        flags: list[str] = []
        if any(paid_but_no_data(bool(p.get("settle_ok")), bool(p.get("http_ok")))
               for p in paid):
            flags.append(FLAG_NO_DELIVERY)
        latencies = sorted(p["latency_ms"] for p in paid
                           if isinstance(p.get("latency_ms"), (int, float)))
        oks = [_parse_ts(p.get("probed_at")) for p in paid if _delivered(p)]
        fails = [_parse_ts(p.get("probed_at")) for p in paid if not _delivered(p)]
        # [MR-3] MPP/Tempo label: known from FREE probes too (T0 parses every
        # live 402), so it aggregates over ALL window probes, not just paid.
        mpp = any(p.get("mpp_option") for p in group)
        # Human-readable identity: last-known serviceName + discovery need.
        named = [p for p in group if p.get("name")]
        needed = [p for p in group if p.get("need")]
        usdg = any(p.get("usdg_option") for p in group)
        # Last-known advertised price — lets estimate_plan price external legs.
        priced = [p for p in group if p.get("price_usdc") is not None]
        priced.sort(key=lambda p: _parse_ts(p.get("probed_at"))
                    or datetime.min.replace(tzinfo=timezone.utc))
        rows.append({
            "resource_url": url,
            "name": named[-1]["name"] if named else None,
            "need": needed[-1]["need"] if needed else None,
            "window_days": window_days,
            "paid_probes": n,
            "delivery_rate": round(rate, 4) if rate is not None else None,
            "delivery_factor": delivery_factor(rate, probed=n > 0),
            "latency_p50_ms": int(latencies[len(latencies) // 2]) if latencies else None,
            "last_ok_at": max(d for d in oks if d).isoformat() if any(oks) else None,
            "last_fail_at": max(d for d in fails if d).isoformat() if any(fails) else None,
            "flags": flags,
            "mpp_option": mpp,
            "usdg_option": usdg,
            "price_usdc": priced[-1]["price_usdc"] if priced else None,
        })
    return rows


def _parse_ts(iso: object) -> Optional[datetime]:
    if not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
