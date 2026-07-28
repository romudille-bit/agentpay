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
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional
from urllib.parse import urlsplit

# Canonical needs list (env PROBER_NEEDS overrides in the runner).
DEFAULT_NEEDS = [
    "web search", "token price", "twitter data", "llm inference",
    "wallet screening", "news", "market data", "pdf ocr",
]

# Plausible request params per need — generic {} gets rejected pre-payment by
# most services ("supply `symbol`…"), making probes unscoreable. These cover
# the common param spellings; unknown keys are ignored by most handlers.
# (First sweep 2026-07-10: 13/15 probes were unscoreable for exactly this.)
NEED_PARAMS: dict[str, dict] = {
    "web search":      {"q": "bitcoin etf inflows", "query": "bitcoin etf inflows"},
    "token price":     {"symbol": "ETH", "token": "ETH"},
    "twitter data":    {"q": "x402", "query": "x402", "username": "coinbase", "name": "coinbase"},
    "llm inference":   {"model": "default",
                        "messages": [{"role": "user", "content": "Reply with the word ok."}],
                        "max_tokens": 16},
    "wallet screening": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "wallet": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    "news":            {"q": "crypto", "query": "crypto", "currencies": "BTC,ETH"},
    "market data":     {"symbol": "BTC", "token": "BTC"},
    "pdf ocr":         {"url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"},
}


def _is_schema_stub(v: object) -> bool:
    """True when a spec value is a JSON-Schema node ({"type": "string"}) rather
    than a usable example value."""
    return isinstance(v, dict) and bool(
        {"type", "const", "enum", "properties", "$ref"} & set(v))


def _spec_fields(spec: object) -> tuple[str, dict]:
    """(method, advertised-field→example) from a seller's input spec. PURE.

    Handles the two shapes Bazaar listings use:
      concrete  {"method":"GET","queryParams":{"url":"https://…/f.pdf"}}
      schema    {"properties":{"input":{"properties":{"body":{"properties":…}}}}}
    Returns ("", {}) for anything unrecognisable — callers fall back to a guess.
    """
    if not isinstance(spec, dict) or not spec:
        return "", {}
    inner = spec.get("properties")
    if isinstance(inner, dict) and isinstance(inner.get("input"), dict):
        spec = inner["input"]                       # unwrap the JSON-Schema form
        inner = spec.get("properties")
    node = inner if isinstance(inner, dict) else spec

    method = ""
    for src in (spec, node):
        raw = src.get("method") if isinstance(src, dict) else None
        if isinstance(raw, dict):                   # schema form: {"const":"GET"}
            raw = raw.get("const") or (raw.get("enum") or [None])[0]
        m = str(raw or "").upper()
        if m in ("GET", "POST"):
            method = m
            break

    fields: dict = {}
    for key in ("queryParams", "body", "params", "query"):
        blob = node.get(key) if isinstance(node, dict) else None
        if isinstance(blob, dict) and isinstance(blob.get("properties"), dict):
            blob = blob["properties"]               # schema form
        if isinstance(blob, dict) and blob:
            fields = dict(blob)
            break
    return method, fields


def call_spec(cand: dict | str | None) -> dict:
    """How to call this candidate: {"method", "params", "source"}. PURE.

    Precedence per field: OUR need-based value (known-good, e.g. a real PDF
    URL) > the seller's own example > drop. Fields the seller did NOT declare
    are dropped — one live listing sets `additionalProperties: false`, and the
    old shotgun approach (sending both `q` and `query`, `symbol` and `token`)
    is exactly what a strict validator rejects.

    AGE-83: on the 2026-07-27 sweep 10 of 12 paid settles were burned on
    pre-delivery param rejections because every probe sent one generic guess
    per need and ignored the call shape the seller published.
    """
    if isinstance(cand, str) or cand is None:
        cand = {"need": cand}
    guess = dict(NEED_PARAMS.get(cand.get("need") or "", {}))
    spec = cand.get("input_spec")
    method, advertised = _spec_fields(spec)
    if not advertised:
        # AGE-87: a spec that declares a METHOD but no fields is an
        # instruction, not an omission — the seller says "call me with
        # nothing." Atlas Market Data declares {"method": "GET"} bare,
        # delivered 3/3 to a bare GET, and 4xx'd the sweep where we
        # appended an uninvited ?symbol=BTC&token=BTC from the need-guess.
        if method:
            return {"method": method, "params": {}, "source": "advertised_empty"}
        return {"method": method, "params": guess,
                "source": "need_guess" if guess else "none"}
    # AGE-87 precedence flip (2026-07-28 regression): the SELLER's example
    # wins — it is known-good by construction, the one request shape they
    # tested. AGE-83 had this backwards (our need-guess first) and burned 5
    # of 8 wasted settles in one sweep on values the listing contradicted:
    # model "default" vs the declared "deepseek-v4-flash", username
    # "coinbase" vs the declared "bankrbot", symbol "ETH" vs the declared
    # "BTC". Our need-guess is the fallback for fields whose example is a
    # placeholder (example.com URLs and the like) or a bare schema stub.
    params: dict = {}
    for key, example in advertised.items():
        if not _is_schema_stub(example) and not _is_placeholder(example):
            params[key] = example
        elif key in guess:
            params[key] = guess[key]
    return {"method": method, "params": params, "source": "advertised"}


# Values sellers publish as stand-ins, not as working inputs. A probe built
# on one of these would test the seller's placeholder, not the seller.
_PLACEHOLDER_MARKERS = ("example.com", "example.org", "example.net",
                        "your_", "your-", "<", "xxxx", "changeme", "lorem ipsum")


def _is_placeholder(v: object) -> bool:
    """Is this advertised example value a stand-in rather than a usable input?
    Non-strings (numbers, dicts, lists) are taken at face value. PURE."""
    if v is None:
        return True
    if not isinstance(v, str):
        return False
    s = v.strip().lower()
    return not s or any(m in s for m in _PLACEHOLDER_MARKERS)


# AGE-87 (root cause 3): catalogue URLs with unsubstituted path params —
# https://x.1x402.sh/api/:name, …/user/:var1 — were probed LITERALLY, so those
# probes could never succeed. Known param names get a plausible value; a URL
# with any unresolvable segment left is unprobeable and must be skipped, not
# paid for.
_PATH_PARAM_VALUES = {
    "name": "coinbase", "handle": "coinbase", "username": "coinbase",
    "user": "coinbase",
    "symbol": "BTC", "token": "BTC", "coin": "BTC", "ticker": "BTC",
    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "wallet": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "model": "gpt-4o-mini",
    "query": "bitcoin", "q": "bitcoin", "topic": "bitcoin",
}

_PATH_PARAM_RE = re.compile(r"(?<=/)(?::(\w+)|\{(\w+)\})(?=/|$|\?)")


def fill_path_template(url: str) -> Optional[str]:
    """Substitute :param / {param} path segments with plausible values. PURE.

    Returns the filled URL, the original URL when it has no templates, or
    None when a segment can't be resolved (e.g. :var1) — the caller must NOT
    pay to probe an unresolvable URL, and must not score the seller for it.
    """
    unresolved = []

    def sub(m: "re.Match[str]") -> str:
        key = (m.group(1) or m.group(2) or "").lower()
        val = _PATH_PARAM_VALUES.get(key)
        if val is None:
            unresolved.append(key)
            return m.group(0)
        return val

    filled = _PATH_PARAM_RE.sub(sub, url)
    return None if unresolved else filled


def params_for(cand: dict | str | None) -> dict:
    """Request params for a candidate (or a bare need string). PURE."""
    return call_spec(cand)["params"]

TOP_K_PER_NEED = 3          # survivors taken per need from rank()
DEFAULT_MAX_PAID = 15       # PROBER_MAX_PAID_PROBES default
WINDOW_DAYS = 30            # scoring window
# Per-probe price ceiling. A single $0.25 endpoint ate half the $0.50 run cap
# on 2026-07-27 and the sweep ended "cap reached" with $0.07 left — one
# premium probe cost us ~5 cheap ones. Above the ceiling a service stays T0
# (free liveness) and unprobed-neutral rather than starving the sweep.
DEFAULT_MAX_PROBE_USD = Decimal("0.05")

# delivery_factor thresholds (PROBER_SPEC "Scoring model")
FACTOR_UNPROBED = 1.0       # neutral — never punish absence of data
FACTOR_GOOD = 1.15          # rate >= 0.9 — modest boost; usage still dominates
FACTOR_BAD = 0.25           # rate < 0.5 — heavy downrank
GOOD_RATE = 0.9
BAD_RATE = 0.5

# AGE-83 (gap 2): with N=1 a delivery_rate is 0.0 or 1.0 and NOTHING else —
# a one-off timeout is indistinguishable from a service that never delivers,
# and a single lucky call is indistinguishable from a reliable one. So a
# single probe moves the factor only halfway: fail-twice-to-bury,
# succeed-twice-to-trust. Both tiers are reported in `confidence` so the
# public leaderboard can say WHY a service sits where it does.
MIN_PROBES_CONFIRMED = 2
FACTOR_PROVISIONAL_BAD = 0.5    # 1 probe, no delivery — downranked, not buried
FACTOR_PROVISIONAL_GOOD = 1.05  # 1 probe, delivered — earned, not yet trusted

# Self-exclusion (enforced, not just policy): the trust oracle must never
# score its own tools — the 1.15× boost on our own board would be
# self-dealing. Was previously emergent (our tools didn't rank for the data
# needs); with the 2026-07-12 Bazaar tag improvements they legitimately can,
# so the ban is now code. AgentPay's presence on /probes comes from real
# customer receipts instead (see /scores.json own_tools).
OWN_HOSTS = ("agentpay.tools", "gateway-production-2cc2.up.railway.app")
OWN_PAYTO = ("0xe8b25a72dd6aef69515452a61ad231c7df2843b7",)


def is_own_service(cand: dict) -> bool:
    host = urlsplit(cand.get("url") or "").netloc.lower()
    pay_to = (cand.get("pay_to") or "").lower()
    return host in OWN_HOSTS or any(pay_to.startswith(p) for p in OWN_PAYTO)


FLAG_NO_DELIVERY = "took_payment_no_delivery"
# One paid_but_no_data = unconfirmed (could be a transient outage that hit our
# probe window). It already hard-drops the service from recommendations —
# buyers are protected immediately — but the PUBLIC accusation ("took payment
# without delivering") waits for a second failure on a separate run.
# AGE-11 decision (Valeria, 2026-07-11): protection immediate, accusation
# confirmed.
FLAG_NO_DELIVERY_UNCONFIRMED = "no_delivery_unconfirmed"


# ── SELECT ─────────────────────────────────────────────────────────────────────

def _dedup_key(cand: dict) -> tuple[str, str]:
    """Dedup by (host, payTo) per spec — one probe per service, not per listing."""
    host = urlsplit(cand.get("url") or "").netloc.lower()
    return (host, (cand.get("pay_to") or "").lower())


def retest_queue(scores: Iterable[dict], limit: int = 6) -> list[dict]:
    """Services whose delivery verdict is not yet settled → re-probe first. PURE.

    AGE-83 (gap 3): the old sweep was one-strike-and-rotate. "PDF to Text"
    failed once on 07-20, was never probed again, and the prober moved on to a
    different provider that is now "trusted" on a single 1/1 success. Neither
    verdict was ever tested twice, so neither is worth anything.

    Priority (a settled verdict is worth more than a new unsettled one):
      1. provisional failures  — one 0.0; confirm the accusation or clear it
      2. provisional successes — one 1.0; earn the trust boost or lose it
      3. confirmed failures     — cheapest possible redemption path, so a
                                  service that fixes itself can climb back
    Already-confirmed successes are NOT re-queued: they enter the sweep through
    normal ranking, and re-paying a proven deliverer buys little.
    """
    tiers: tuple[list[dict], list[dict], list[dict]] = ([], [], [])
    for row in scores or ():
        url = (row or {}).get("resource_url")
        n = row.get("paid_probes") or 0
        rate = row.get("delivery_rate")
        if not url or not n or rate is None:
            continue
        cand = {
            "url": url,
            "name": row.get("name") or url,
            "pay_to": (row.get("pay_to") or "").lower(),
            "network": row.get("network") or "",
            "need": row.get("need"),
            "price_usd": _as_decimal(row.get("price_usdc")),
            "retest": True,
        }
        if n < MIN_PROBES_CONFIRMED:
            tiers[0 if rate < GOOD_RATE else 1].append(cand)
        elif rate < BAD_RATE:
            tiers[2].append(cand)
    out = [c for tier in tiers for c in tier]
    return out[:limit] if limit else out


def _as_decimal(v: object) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _round_robin(ranked: dict[str, list[dict]], top_k: int) -> list[tuple[dict, str]]:
    """Interleave needs: every need's #1 candidate before any need's #2. PURE.

    AGE-83 (gap 5): the old loop was `for need in sorted(ranked)` — strictly
    alphabetical — so with 8 needs × top_k 3 against a 15-probe cap, the first
    five needs consumed every paid slot and "wallet screening" / "web search"
    never got a paid probe at all. A per-need delivery leaderboard is
    impossible if whole needs are never paid.
    """
    out: list[tuple[dict, str]] = []
    for i in range(top_k):
        for need in sorted(ranked):
            row = ranked[need]
            if i < len(row):
                out.append((row[i], need))
    return out


def select_candidates(
    ranked: dict[str, list[dict]],
    recent: Iterable[dict] = (),
    max_paid: int = DEFAULT_MAX_PAID,
    top_k: int = TOP_K_PER_NEED,
    retest: Iterable[dict] = (),
    max_probe_usd: Optional[Decimal] = DEFAULT_MAX_PROBE_USD,
) -> dict[str, list[dict]]:
    """Budget-bounded, deterministic candidate selection. PURE.

    `ranked` maps need → rank()['results'] (already junk-filtered survivors,
    quality-sorted). `recent` is any service verified_route recommended in the
    last 7 days (freshness guarantee — never recommend something unprobed).
    `retest` is retest_queue() output — unsettled verdicts, probed first.

    Paid-set priority: retest → recent recommendations → round-robin over
    needs. Candidates priced above `max_probe_usd` are T0-only (see
    DEFAULT_MAX_PROBE_USD) and returned under "too_expensive" so the run can
    say what it declined instead of silently covering less.

    Returns {"t1": capped paid set, "t0": full deduped set, "too_expensive":
    [...]} — T0 free probes run on everything regardless (they cost nothing).
    """
    paid: list[dict] = []
    t0: list[dict] = []
    too_expensive: list[dict] = []
    seen: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()

    # A retest row from /scores.json knows the URL but not the seller's
    # advertised call shape; today's ranking does. Merge so a re-probe is at
    # least as well-formed as a first probe.
    by_url = {c.get("url"): c for row in ranked.values() for c in row if c.get("url")}

    def add(cand: dict, need: Optional[str]) -> None:
        if not cand or not cand.get("url"):
            return
        known = by_url.get(cand["url"]) or {}
        # Enrich BEFORE deduping. A retest row comes from /scores.json, which
        # publishes no pay_to, so its dedup key was (host, "") — a different
        # key from the same service's (host, "0x…") in today's ranking, and
        # the sweep paid DeepSeek twice in one run.
        # cand wins on identity/need; the ranked row fills in what it can't
        # know. "" counts as absent, not as an override — a score row carries
        # pay_to="" and must not blank out the address the ranking has.
        cand = {**known,
                **{k: v for k, v in cand.items() if v is not None and v != ""},
                "need": need}
        if is_own_service(cand):        # enforced self-exclusion
            return
        key = _dedup_key(cand)
        if key in seen or cand["url"] in seen_urls:
            return
        seen.add(key)
        seen_urls.add(cand["url"])
        t0.append(cand)
        # rank()'s public projection stringifies price_usd; ledger and score
        # inputs carry Decimal or None. Normalise before comparing.
        price = _as_decimal(cand.get("price_usd"))
        if max_probe_usd is not None and price is not None and price > max_probe_usd:
            too_expensive.append(cand)
            return
        if len(paid) < max_paid:
            paid.append(cand)

    # Unsettled verdicts FIRST — a second data point on a service we already
    # paid is worth more than a first on one we haven't (AGE-83 gap 2/3).
    for cand in retest:
        add(cand, need=cand.get("need") or "retest")
    # Recent recommendations next — the freshness guarantee outranks sweep order.
    for cand in recent:
        add(cand, need="recently recommended")
    for cand, need in _round_robin(ranked, top_k):
        add(cand, need=need)
    # T0 breadth: free probes cost nothing, so the WHOLE survivor list gets a
    # T0 check (alive / wellformed / price / rails) — only the top-k enter the
    # paid set. This is what makes the leaderboard grow faster than the budget.
    for need in sorted(ranked):
        for cand in ranked[need][top_k:]:
            if not cand or not cand.get("url") or is_own_service(cand):
                continue
            key = _dedup_key(cand)
            if key in seen or cand["url"] in seen_urls:
                continue
            seen.add(key)
            seen_urls.add(cand["url"])
            t0.append({**cand, "need": need})

    return {"t1": paid, "t0": t0, "too_expensive": too_expensive}


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
# AGE-83: `usdg_option` was structurally always False. The marker list said
# "eip155:46630", but every live USDG listing (ArbiPulse, Agent402.tools,
# Concierge Agent — 5 options in the 2026-07-27 catalog) advertises
# `eip155:4663` with asset 0x5fc5360D…d168 and extra.name "Global Dollar".
# "eip155:4663" is also a prefix of "46630", so keeping both costs nothing.
_USDG_MARKERS = ("usdg", "eip155:4663", "eip155:46630", "robinhood",
                 "global dollar", "0x5fc5360d0400a0fd4f2af552add042d716f1d168")


def _option_hay(opt: dict) -> str:
    """Searchable text for rail labels. Includes `extra.name` — the human asset
    name ("Global Dollar", "USD Coin") is often the only place the rail is
    spelled out, and leaving it out is half of why USDG never matched."""
    parts = [str(opt.get(k, "")) for k in
             ("network", "scheme", "rail", "chain", "asset", "protocol")]
    extra = opt.get("extra")
    if isinstance(extra, dict):
        parts.append(str(extra.get("name", "")))
    return " ".join(parts).lower()


def _is_mpp_option(opt: dict) -> bool:
    """[MR-3] Does this option advertise MPP/Tempo? Detection only, never settled.

    AGE-83 (gap 6) verified 2026-07-27: `mpp_options: 0` on every sweep is a
    REAL read, not a stubbed field. A catalog sweep of 96 unique listings /
    229 payment options across 14 networks (Base, Solana, Polygon, Arbitrum,
    World, Avalanche, Monad, Celo, Stellar, Algorand, XRPL, …) found ZERO
    advertising MPP or Tempo. The parser is exercised on every live 402; the
    marketplace simply doesn't offer the rail yet.
    """
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


def _schema_keys(out_schema: dict | list | None) -> list[str]:
    """Advertised top-level keys from an outputSchema-ish dict. Tolerant of the
    common shapes: JSON-schema {properties: {...}}, bazaar info.output {...},
    a plain example object — or an already-extracted list of key names, which
    is what rank()'s `output_keys` hands us (AGE-83)."""
    if isinstance(out_schema, list):
        return [k for k in out_schema if isinstance(k, str)]
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
    """The worst outcome: took money, gave nothing back at the HTTP level.

    NOTE (AGE-83): this is the NARROW test — settled but no 200. It misses the
    other half of "took payment, delivered nothing": a 200 carrying an empty
    body or a payload that doesn't match the advertised schema. Use
    `took_payment_no_delivery()` for the flag; this stays for the raw
    settle-vs-HTTP distinction.
    """
    return bool(settle_ok and not http_ok)


def took_payment_no_delivery(p: dict) -> bool:
    """Did this paid probe settle and fail to deliver? PURE.

    The flag must use the SAME definition of delivery as delivery_rate does,
    or the summary contradicts the scores. It used to be `settled ∧ ¬HTTP-200`,
    which is strictly narrower: X (Twitter) JSON API sat at delivery_rate 0.0
    over 5 paid probes and PDF to Text at 0.0 over 3, and BOTH carried zero
    flags — every sweep reported "0 flagged took_payment_no_delivery" while
    the score table showed four services at 0.0. A ledger skim read as a clean
    bill of health. Money left the wallet and nothing usable came back: that
    is the failure, whatever HTTP status dressed it up.
    """
    return bool(p.get("settle_ok")) and not _delivered(p)


# ── SCORE ──────────────────────────────────────────────────────────────────────

def delivery_factor(rate: Optional[float], probed: bool,
                    n_probes: Optional[int] = None) -> float:
    """PROBER_SPEC scoring model. PURE.

        unprobed              → 1.00  (neutral)
        1 probe,  delivered   → 1.05  (provisional — succeed-twice-to-trust)
        1 probe,  no delivery → 0.50  (provisional — fail-twice-to-bury)
        ≥2 probes, rate ≥ 0.9 → 1.15
        ≥2 probes, 0.5–0.9    → 1.00 − 0.5×(0.9 − rate)
        ≥2 probes, < 0.5      → 0.25

    `n_probes` is optional for backwards compatibility; omitting it keeps the
    pre-AGE-83 confirmed-tier behaviour.
    """
    if not probed or rate is None:
        return FACTOR_UNPROBED
    if n_probes is not None and n_probes < MIN_PROBES_CONFIRMED:
        return FACTOR_PROVISIONAL_GOOD if rate >= GOOD_RATE else FACTOR_PROVISIONAL_BAD
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
        # AGE-86: DELIVERY IS A CLAIM ABOUT WHAT HAPPENS AFTER MONEY MOVES.
        # A probe that never settled — DNS failure, connection refused, a
        # payment the seller's facilitator rejected — says nothing about
        # whether the seller delivers, so it must never enter delivery_rate.
        # Before this gate, X (Twitter) JSON API sat publicly at 0.25×
        # "confirmed" over SIX probes in which no payment was EVER
        # transmitted (its host didn't resolve), and the flag stayed at 0
        # for all 82 services because every 0.0 in the corpus was a settle
        # failure, not a delivery failure. Settle failures are kept as their
        # own signal (`settle_failures`), labelled as what they are.
        settled = [p for p in paid if p.get("settle_ok")]
        n = len(settled)
        settle_failures = len(paid) - n
        delivered = sum(1 for p in settled if _delivered(p))
        rate = (delivered / n) if n else None
        flags: list[str] = []
        # AGE-83: same delivery definition as delivery_rate — a settled probe
        # that returns 200-with-nothing is a non-delivery, not a clean run.
        no_delivery = sum(1 for p in settled if took_payment_no_delivery(p))
        if no_delivery >= MIN_PROBES_CONFIRMED:
            flags.append(FLAG_NO_DELIVERY)              # confirmed → public ⚠
        elif no_delivery == 1:
            flags.append(FLAG_NO_DELIVERY_UNCONFIRMED)  # rec-drop only
        latencies = sorted(p["latency_ms"] for p in settled
                           if isinstance(p.get("latency_ms"), (int, float)))
        oks = [_parse_ts(p.get("probed_at")) for p in settled if _delivered(p)]
        fails = [_parse_ts(p.get("probed_at")) for p in settled if not _delivered(p)]
        # [MR-3] MPP/Tempo label: known from FREE probes too (T0 parses every
        # live 402), so it aggregates over ALL window probes, not just paid.
        mpp = any(p.get("mpp_option") for p in group)
        # Human-readable identity: last-known serviceName + discovery need +
        # settlement network (from the service's advertised accepts).
        named = [p for p in group if p.get("name")]
        needed = [p for p in group if p.get("need")]
        networked = [p for p in group if p.get("network")]
        usdg = any(p.get("usdg_option") for p in group)
        # Last-known advertised price — lets estimate_plan price external legs.
        priced = [p for p in group if p.get("price_usdc") is not None]
        priced.sort(key=lambda p: _parse_ts(p.get("probed_at"))
                    or datetime.min.replace(tzinfo=timezone.utc))
        rows.append({
            "resource_url": url,
            "name": named[-1]["name"] if named else None,
            "need": needed[-1]["need"] if needed else None,
            "network": networked[-1]["network"] if networked else None,
            "window_days": window_days,
            # paid_probes keeps its public meaning — probes where we actually
            # paid. Settle failures are counted separately, never as delivery.
            "paid_probes": n,
            "settle_failures": settle_failures,
            "delivery_rate": round(rate, 4) if rate is not None else None,
            "delivery_factor": delivery_factor(rate, probed=n > 0, n_probes=n),
            # AGE-83: how much this row is worth believing. "provisional" = a
            # single paid probe, so 0.0/1.0 carries no signal about whether the
            # next call would work; the re-probe queue targets these first.
            "confidence": (None if not n else
                           "confirmed" if n >= MIN_PROBES_CONFIRMED else "provisional"),
            "no_delivery_probes": no_delivery,
            "latency_p50_ms": int(latencies[len(latencies) // 2]) if latencies else None,
            "last_ok_at": max(d for d in oks if d).isoformat() if any(oks) else None,
            "last_fail_at": max(d for d in fails if d).isoformat() if any(fails) else None,
            "flags": flags,
            "mpp_option": mpp,
            "usdg_option": usdg,
            "price_usdc": priced[-1]["price_usdc"] if priced else None,
        })
    return rows


def need_leaderboard(scores: Iterable[dict],
                     min_probes: int = 1) -> dict[str, list[dict]]:
    """Per-need delivery ranking: "for pdf-ocr, A delivers and B doesn't". PURE.

    AGE-83 (gap 5): usage ranking says what's popular; only a paid comparison
    of two providers for the SAME need says what works. That comparison is
    exactly what a buyer pays verified_route for, and it was impossible while
    the sweep paid at most one provider per need.

    Only paid-probed services appear — an unprobed service is neutral, not
    last. Ordered best-first: delivery rate desc, confirmed above provisional,
    then faster p50.
    """
    board: dict[str, list[dict]] = {}
    for row in scores or ():
        n = (row or {}).get("paid_probes") or 0
        if n < min_probes or row.get("delivery_rate") is None:
            continue
        board.setdefault(row.get("need") or "uncategorised", []).append({
            "name": row.get("name"),
            "resource_url": row.get("resource_url"),
            "delivery_rate": row.get("delivery_rate"),
            "paid_probes": n,
            "confidence": row.get("confidence"),
            "latency_p50_ms": row.get("latency_p50_ms"),
            "price_usdc": row.get("price_usdc"),
            "flags": row.get("flags") or [],
        })
    for need, rows in board.items():
        rows.sort(key=lambda r: (
            -(r.get("delivery_rate") or 0.0),
            0 if r.get("confidence") == "confirmed" else 1,
            r.get("latency_p50_ms") if r.get("latency_p50_ms") is not None else 10**9,
        ))
    return dict(sorted(board.items()))


def reconcile_settlements(entries: Iterable[dict],
                          transfers: Iterable[dict]) -> list[dict]:
    """Match a run receipt's payment entries against on-chain USDC transfers
    from the prober wallet. PURE — the I/O lives in
    tools/reconcile_prober_spend.py.

    AGE-88: an `uncertain_settlement` entry means a signed EIP-3009 auth left
    the wire and the seller answered non-200 — the SDK fails closed and counts
    the spend, but nobody knows whether the seller actually settled. The
    answer IS payer-observable: `transferWithAuthorization` emits a normal
    ERC-20 Transfer event from our wallet regardless of who submitted the tx,
    so the wallet's token-transfer history is ground truth.

    entries:   receipt `breakdown` rows — {"tool", "cost" ("$0.0075"),
               "state" ("settled" | "uncertain_settlement" | …)}
    transfers: token txs FROM the wallet — {"to", "value" (atomic 6dp str),
               "hash", "timeStamp"?} — pre-filtered by the caller to the run's
               time window.

    Confirmed-settled entries anchor their transfers first, so a successful
    $0.05 call can't lend its on-chain evidence to an uncertain $0.05 one.
    Returns one row per entry: {"tool", "cost", "state", "resolution":
    "confirmed" | "settled_on_chain" | "no_onchain_evidence", "tx_hash"?}.
    """
    def _atomic(cost: object) -> Optional[int]:
        try:
            return int(Decimal(str(cost).lstrip("$")) * 1_000_000)
        except (InvalidOperation, ValueError):
            return None

    pool: list[dict] = [dict(t) for t in transfers]

    def _claim(amount: Optional[int]) -> Optional[dict]:
        for i, t in enumerate(pool):
            try:
                if int(Decimal(str(t.get("value")))) == amount:
                    return pool.pop(i)
            except (InvalidOperation, ValueError, TypeError):
                continue
        return None

    entries = list(entries)
    out: list[dict] = [dict(tool=e.get("tool"), cost=e.get("cost"),
                            state=e.get("state")) for e in entries]
    # Pass 1 — anchor confirmed settles to their transfers.
    for e, o in zip(entries, out):
        if e.get("state") == "settled" or e.get("success"):
            t = _claim(_atomic(e.get("cost")))
            o["resolution"] = "confirmed"
            o["tx_hash"] = (t or {}).get("hash") or e.get("tx_hash") or None
    # Pass 2 — resolve the uncertain ones against what's left.
    for e, o in zip(entries, out):
        if "resolution" in o:
            continue
        t = _claim(_atomic(e.get("cost")))
        if t is not None:
            o["resolution"] = "settled_on_chain"   # they DID take the money
            o["tx_hash"] = t.get("hash")
        else:
            o["resolution"] = "no_onchain_evidence"
    return out


def _parse_ts(iso: object) -> Optional[datetime]:
    if not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
