#!/usr/bin/env python3
"""
run.py — the AgentPay Active Prober ("did it actually deliver?").

Delivery-quality telemetry for verified_route: spends pennies probing the
x402 marketplace as a REAL paying customer, then scores each service on
whether it delivered — the axis usage-ranking can't see (PROBER_SPEC).

Each run:
  1. SELECT  — unsettled verdicts to re-probe + recent verified_route
               recommendations + canonical needs × rank() top-K, interleaved
               round-robin → deduped candidates, paid set capped
  2. T0      — free probes on ALL candidates: alive? 402 well-formed?
               price honest? MPP option advertised? [MR-3]
  3. T1      — paid probes on the capped set via Session.call(url), inside a
               hard Session(max_spend=) cap — a runaway probe loop is
               physically impossible
  4. SCORE   — pure aggregation → per-service delivery rows
  5. PUBLISH — probe receipts are real receipts; the run posts to the existing
               flagship ingest with goal="probe_sweep" so /ledger shows the
               reasoning. (Raw service_probes/service_scores Supabase rows
               land with AGE-6.)

Identity & config (env):
  PROBER_BASE_KEY         — funded Base/EVM key (~$10) — the only funded key
  PROBER_STELLAR_SECRET   — optional; ephemeral keypair minted when unset
                            (fresh identity per run — anti-special-casing)
  PROBER_MAX_SPEND        — hard cap per run in USDC (default "0.50")
  PROBER_MAX_PAID_PROBES  — cap on paid probes per run (default 15)
  PROBER_MAX_PROBE_USD    — per-probe price ceiling (default "0.05"); above it
                            a service stays T0-only, so one premium endpoint
                            can't eat the run cap (AGE-83)
  PROBER_RETEST_MAX       — unsettled verdicts to re-probe per run (default 6;
                            0 disables re-probing)
  PROBER_NEEDS            — comma list overriding the canonical needs
  PROBER_JITTER_MAX_S     — random start delay in seconds (default 0; the
                            Railway cron sets this for ±jitter)
  FLAGSHIP_INGEST_SECRET  — reused for the ledger ingest POST
  AGENTPAY_GATEWAY_URL    — override gateway (default https://agentpay.tools)

Exit codes: 0 = sweep published; 1 = run failed (Railway cron surfaces it).
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

GATEWAY = os.environ.get("AGENTPAY_GATEWAY_URL", "https://agentpay.tools")

# Generic UA (anti-special-casing): a seller shouldn't be able to greet the
# prober differently from any other x402 buyer.
PROBE_UA = "Mozilla/5.0 (compatible; x402-client)"

try:
    import agentpay  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from agents.prober import probe
except ModuleNotFoundError:
    import probe  # type: ignore

from gateway import radar


def log(msg: str) -> None:
    print(f"[prober] {msg}", flush=True)


# ── SELECT inputs (I/O) ────────────────────────────────────────────────────────

def _needs_from_env() -> list[str]:
    raw = os.environ.get("PROBER_NEEDS", "")
    needs = [n.strip() for n in raw.split(",") if n.strip()]
    return needs or list(probe.DEFAULT_NEEDS)


def rank_needs(needs: list[str], budget: Decimal) -> dict[str, list[dict]]:
    """rank() each need against Bazaar. Best-effort per need — one dead query
    never kills the sweep."""
    ranked: dict[str, list[dict]] = {}
    for need in needs:
        try:
            ranked[need] = radar.rank(need, budget)["results"]
            log(f"rank '{need}': {len(ranked[need])} survivors")
        except Exception as e:
            log(f"rank '{need}' failed: {e}")
            ranked[need] = []
    return ranked


def recent_recommendations(days: int = 7) -> list[dict]:
    """Services verified_route actually recommended in the last `days` —
    the freshness guarantee. Reads the public ledger; best-effort ([] on any
    failure). Recommendation public shape → candidate dict."""
    try:
        req = urllib.request.Request(f"{GATEWAY}/ledger.json",
                                     headers={"User-Agent": PROBE_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        log(f"ledger fetch failed (recent recs skipped): {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for run in (data.get("runs") or []) if isinstance(data, dict) else []:
        ts = probe._parse_ts((run or {}).get("run_at_iso") or (run or {}).get("run_at"))
        if ts is None or ts < cutoff:
            continue
        rec = (((run.get("findings") or {}).get("vetting") or {}).get("recommendation")) or {}
        if not rec.get("url"):
            continue
        price = rec.get("price_usd")
        try:
            price_usd = Decimal(str(price)) if price is not None else None
        except (InvalidOperation, ValueError):
            price_usd = None
        out.append({
            "name": rec.get("name") or rec.get("url"),
            "url": rec["url"],
            "pay_to": (rec.get("pay_to") or "").lower(),
            "price_usd": price_usd,
            "network": rec.get("network", ""),
            "accepts": (rec.get("ready_to_pay") or {}).get("accepts") or {},
        })
    if out:
        log(f"recent verified_route recs: {len(out)}")
    return out


def current_scores() -> list[dict]:
    """The published 30d delivery scores — the input to the re-probe queue.

    The runner holds no history (it's a credential-free HTTP customer), so it
    reads its own public output to find out which verdicts are still resting on
    a single probe. Best-effort: [] on any failure means "no re-probes this
    run", never a failed sweep."""
    try:
        req = urllib.request.Request(f"{GATEWAY}/scores.json",
                                     headers={"User-Agent": PROBE_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        return [s for s in (data.get("services") or []) if isinstance(s, dict)]
    except Exception as e:
        log(f"scores fetch failed (re-probes skipped): {e}")
        return []


# ── Probes (I/O) ───────────────────────────────────────────────────────────────

def probe_free(cand: dict) -> dict:
    """T0: GET the resource, judge the live 402 (body + PAYMENT-REQUIRED
    header). Never pays."""
    status, body, headers = None, None, None
    error = None
    try:
        req = urllib.request.Request(cand["url"], headers={"User-Agent": PROBE_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            status, body, headers = r.status, r.read().decode(errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:  # 402 arrives here
        status, body, headers = e.code, e.read().decode(errors="replace"), dict(e.headers)
    except Exception as e:
        error = str(e)[:200]
    checks = probe.t0_checks(status, body, cand.get("price_usd"), headers=headers)
    return _probe_row(cand, "free", error=error, **checks)


def probe_paid(session, cand: dict) -> dict:
    """T1: settle a real payment via the SDK, judge delivery. The Session cap
    is the only spend authority — would_exceed() gates before every call.

    AGE-86 contract: the ONLY rows this returns un-skipped are ones where a
    payment verifiably settled (success or RefundPending). Everything else is
    classified into an `outcome` and marked skipped — raw evidence, never a
    delivery observation. Fail closed: when we cannot prove money moved, we
    do not publish a claim about the seller.
    """
    from agentpay import PaymentFailed, PrePaymentError, RefundPending
    try:
        from agentpay import UnsupportedChainPayment
    except ImportError:                       # older SDK without the typed class
        UnsupportedChainPayment = ()

    price = cand.get("price_usd") or Decimal("0.01")
    if session.would_exceed(price):
        return _probe_row(cand, "paid", error="skipped: cap reached",
                          skipped=True, outcome="cap_reached")

    # AGE-87: URLs with unsubstituted path params (…/api/:name) were probed
    # LITERALLY — X (Twitter) JSON API accumulated six "failures" against a
    # template that was never a real endpoint. Fill known params; refuse to
    # pay for an unresolvable one.
    url = probe.fill_path_template(cand["url"])
    if url is None:
        return _probe_row(cand, "paid", skipped=True,
                          outcome="unfilled_path_template",
                          error="skipped: URL path template has unresolvable "
                                "params — not probeable as listed")

    settle_ok = http_ok = False
    latency_ms = None
    tx_hash = None
    error = None
    data = None
    # The seller's own advertised call shape when it published one, a
    # per-need guess otherwise (AGE-83/AGE-87 — seller's example wins).
    spec = probe.call_spec(cand)
    t0 = time.monotonic()
    try:
        r = session.call(url, spec["params"])
        latency_ms = int((time.monotonic() - t0) * 1000)
        settle_ok = http_ok = True
        data = getattr(r, "data", r)
        tx_hash = getattr(r, "tx", None)
    except UnsupportedChainPayment as e:
        # Seller only settles on chains our wallet can't (e.g. Avalanche
        # eip155:43114). Unmet demand on an unsupported chain, NOT a delivery
        # failure — record the chain (authoritative, from the live 402) and
        # mark the row unscoreable so it never enters delivery_rate or the
        # FATAL 0/N denominator. Surfaced as a grant signal by AGE-81.
        latency_ms = int((time.monotonic() - t0) * 1000)
        _chains = list(getattr(e, "offered_networks", None) or [])
        return _probe_row(
            cand, "paid",
            error=f"unsupported chain: {', '.join(_chains) or '?'}",
            skipped=True, latency_ms=latency_ms, unsupported_chain=_chains,
            outcome="unsupported_chain",
        )
    except PrePaymentError as e:
        # AGE-86: nothing was paid — DNS failure, connection refused, a non-402
        # response, an unparseable 402. This is a statement about reachability
        # (ours or theirs), never about delivery. Before this clause the
        # generic handler scored these 0.0 against the seller: X (Twitter)
        # JSON API reached 0.25× "confirmed" on six DNS failures.
        latency_ms = int((time.monotonic() - t0) * 1000)
        return _probe_row(cand, "paid", error=f"pre-payment: {str(e)[:200]}",
                          skipped=True, latency_ms=latency_ms,
                          outcome="unreachable", param_source=spec["source"])
    except PaymentFailed as e:
        # Our payment didn't complete (signing, wallet, broadcast). Money may
        # not have moved; the fault axis is payment, not delivery.
        latency_ms = int((time.monotonic() - t0) * 1000)
        return _probe_row(cand, "paid", error=f"settle failed: {str(e)[:200]}",
                          skipped=True, latency_ms=latency_ms,
                          outcome="settle_failed", param_source=spec["source"])
    except RefundPending as e:
        # Payment settled but the tool failed to deliver → the worst outcome,
        # and the ONE unsettled-looking case that stays scoreable: money
        # provably moved.
        latency_ms = int((time.monotonic() - t0) * 1000)
        settle_ok, http_ok = True, False
        error = f"paid, no delivery (refund pending): {str(e)[:200]}"
    except Exception as e:
        # Post-transmission rejections land here ("settlement uncertain,
        # spend recorded: 4xx/5xx…"): the signed auth left the wire, the
        # seller answered non-200, and we cannot prove whether they settled.
        # AGE-86: fail closed — unscoreable, whatever the status code. The
        # old guard matched only "spend recorded): 4", so DeepSeek's 502
        # (relaying a 400 WE caused with model="default") fell through and
        # was scored 0.0 against them. AGE-88 reconciles these on-chain;
        # if one settled AND returned nothing, THAT is a took-payment case
        # to promote — with evidence, not by default.
        latency_ms = int((time.monotonic() - t0) * 1000)
        error = str(e)[:200]
        outcome = ("payment_rejected"
                   if ("spend recorded" in error or "settlement uncertain" in error
                       or "no payment settled" in error)
                   else "unreachable")
        return _probe_row(cand, "paid", error=error, skipped=True,
                          latency_ms=latency_ms, outcome=outcome,
                          param_source=spec["source"])

    # What the seller said comes BACK. rank()'s public projection carries
    # `output_keys`; ledger-sourced recommendations carry a raw `accepts`.
    # Before AGE-83 ranked candidates had neither, so nearly every paid probe
    # judged delivery on "non-empty" alone — and a 200 carrying
    # {"error": "…"} passed as delivered.
    out_schema = (cand.get("output_keys")
                  or (cand.get("accepts") or {}).get("outputSchema"))
    checks = probe.t1_evaluate(data, out_schema) if http_ok else \
        {"response_nonempty": False, "schema_ok": None}
    return _probe_row(
        cand, "paid", error=error, outcome="settled",
        settle_ok=settle_ok, http_ok=http_ok, latency_ms=latency_ms,
        tx_hash=tx_hash, param_source=spec["source"], **checks,
    )


def _probe_row(cand: dict, probe_type: str, **fields) -> dict:
    price = cand.get("price_usd")
    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "resource_url": cand["url"],
        "name": cand.get("name"),
        "need": cand.get("need"),
        "pay_to": cand.get("pay_to"),
        "network": cand.get("network"),
        "price_usdc": str(price) if price is not None else None,
        "probe_type": probe_type,
        **fields,
    }


# ── PUBLISH ────────────────────────────────────────────────────────────────────

def publish_run(probes: list[dict], run: dict) -> dict | None:
    """POST the sweep to the gateway's prober ingest: raw rows land in
    service_probes, the gateway rebuilds service_scores over the FULL 30d
    window (it holds the history; this runner only has today's rows), and
    the flagship-style `run` summary feeds /ledger reasoning.

    Returns the ingest response (incl. authoritative window scores) or None.
    Best-effort; no-op when FLAGSHIP_INGEST_SECRET is unset."""
    secret = os.environ.get("FLAGSHIP_INGEST_SECRET", "")
    if not secret:
        log("ingest skipped — FLAGSHIP_INGEST_SECRET unset")
        return None
    try:
        req = urllib.request.Request(
            f"{GATEWAY}/v1/prober/run",
            data=json.dumps({"probes": probes, "run": run}, default=str).encode(),
            headers={"Content-Type": "application/json",
                     "X-Flagship-Secret": secret,
                     # Cloudflare 403s python-urllib's default UA (live
                     # incident, first sweep 2026-07-10) — send ours.
                     "User-Agent": PROBE_UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
            ok = 200 <= resp.status < 300
        log(f"ingest {'ok' if ok else 'partial'} → /v1/prober/run | "
            f"probes_stored={body.get('probes_stored')} "
            f"scores_stored={body.get('scores_stored')} "
            f"window_rows={body.get('window_rows')}")
        return body
    except Exception as e:
        log(f"ingest failed: {e}")
        return None


# ── The run ────────────────────────────────────────────────────────────────────

def main() -> int:
    from agentpay import AgentWallet, Session
    from stellar_sdk import Keypair

    base_key = os.environ.get("PROBER_BASE_KEY", "")
    if not base_key:
        log("FATAL: PROBER_BASE_KEY is required (funded Base wallet)")
        return 1

    jitter = float(os.environ.get("PROBER_JITTER_MAX_S", "0") or 0)
    if jitter > 0:
        delay = random.uniform(0, jitter)
        log(f"jitter sleep {delay:.0f}s")
        time.sleep(delay)

    max_spend = os.environ.get("PROBER_MAX_SPEND", "0.50")
    max_paid = int(os.environ.get("PROBER_MAX_PAID_PROBES", str(probe.DEFAULT_MAX_PAID)))
    max_probe = Decimal(os.environ.get("PROBER_MAX_PROBE_USD",
                                       str(probe.DEFAULT_MAX_PROBE_USD)))
    retest_max = int(os.environ.get("PROBER_RETEST_MAX", "6"))
    needs = _needs_from_env()

    # Fresh identity per run unless pinned — the funded key is Base-only.
    stellar_secret = os.environ.get("PROBER_STELLAR_SECRET", "") or Keypair.random().secret
    wallet = AgentWallet(secret_key=stellar_secret, network="mainnet", base_key=base_key)
    if not wallet.base_address:
        # A broken BUYER-side wallet must never produce probe rows: every T1
        # would fail at settle and innocent sellers would be scored 0.0/0.25.
        # (Live incident, first dry run 2026-07-10: an address was passed
        # instead of a private key.) Fail loudly instead.
        log(f"FATAL: Base wallet unavailable ({wallet.base_disabled_reason}) — "
            "PROBER_BASE_KEY must be the 0x… PRIVATE key (66 chars), not the address")
        return 1
    s = Session(wallet=wallet, gateway_url=GATEWAY, max_spend=max_spend)
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_at_iso = datetime.now(timezone.utc).isoformat()
    log(f"run start {run_at} | wallet {wallet.base_address} | cap ${max_spend} | "
        f"max paid probes {max_paid} | needs {len(needs)}")

    # 1. SELECT
    ranked = rank_needs(needs, Decimal(str(max_spend)))
    recent = recent_recommendations()
    retest = probe.retest_queue(current_scores(), limit=retest_max) if retest_max else []
    if retest:
        log(f"re-probing {len(retest)} unsettled verdict(s): "
            + ", ".join(str(c.get("name"))[:28] for c in retest))
    sel = probe.select_candidates(ranked, recent, max_paid=max_paid,
                                  retest=retest, max_probe_usd=max_probe)
    log(f"candidates: {len(sel['t0'])} total, {len(sel['t1'])} paid-eligible")
    if sel["too_expensive"]:
        # Never silently cover less: a skipped premium endpoint is a coverage
        # gap the reader deserves to see, not an absence in the table.
        log(f"{len(sel['too_expensive'])} above the ${max_probe} per-probe ceiling "
            "— T0 only, left unprobed-neutral: "
            + ", ".join(f"{c.get('name')} (${c.get('price_usd')})"
                        for c in sel["too_expensive"][:5]))

    # 2. T0 — free probes on everything
    probes: list[dict] = []
    for cand in sel["t0"]:
        row = probe_free(cand)
        probes.append(row)
    t0_alive = sum(1 for p in probes if p.get("alive"))
    t0_wf = sum(1 for p in probes if p.get("x402_wellformed"))
    t0_mpp = sum(1 for p in probes if p.get("mpp_option"))
    log(f"T0 done: {t0_alive}/{len(sel['t0'])} alive, {t0_wf} well-formed, "
        f"{t0_mpp} advertise MPP [MR-3]")

    # 3. T1 — paid probes, cap-gated. EVERY row is logged, skipped or not —
    # on 2026-07-28, 8 of 13 paid settles left no trace anywhere (not logged,
    # not stored), and the entire post-mortem had to be reconstructed by
    # inference from the run receipt (AGE-87 root cause 4).
    paid_rows: list[dict] = []
    for cand in sel["t1"]:
        row = probe_paid(s, cand)
        paid_rows.append(row)
        err = f" | err: {row['error']}" if row.get("error") else ""
        if row.get("skipped"):
            log(f"T1 {cand['name']}: UNSCOREABLE ({row.get('outcome')}) "
                f"params={row.get('param_source', '?')}{err}")
        else:
            log(f"T1 {cand['name']}: settle={row.get('settle_ok')} "
                f"http={row.get('http_ok')} nonempty={row.get('response_nonempty')} "
                f"schema={row.get('schema_ok')} {row.get('latency_ms')}ms "
                f"| tx {row.get('tx_hash')}{err}")

    # Systemic buyer-side guard: if payments were attempted and NONE settled,
    # the problem is almost certainly ours (SDK, wallet, funds, network) —
    # abort without publishing rather than scoring 15 innocent sellers 0.0.
    # (Under AGE-86 skipped rows can't hurt sellers anyway; this guard now
    # protects the SPEND, not the scores.)
    attempted = [r for r in paid_rows
                 if r.get("outcome") in ("settled", "payment_rejected",
                                         "settle_failed")]
    if attempted and not any(r.get("settle_ok") for r in attempted):
        log(f"FATAL: 0/{len(attempted)} paid probes settled — treating as a "
            "buyer-side systemic failure; NOT publishing seller scores")
        return 1
    probes.extend(paid_rows)

    # AGE-80: sellers that only settle on chains we can't (e.g. Avalanche
    # eip155:43114) are recorded, never scored. Surface them as unmet demand —
    # a discovery/grant signal (persisted + shown on /probes by AGE-81).
    _unsupported = [r for r in paid_rows if r.get("unsupported_chain")]
    if _unsupported:
        _uchains = sorted({c for r in _unsupported for c in r["unsupported_chain"]})
        log(f"unsettleable-chain demand: {len(_unsupported)} paid service(s) on "
            f"{_uchains} — no settlement rail, tracked as unmet demand, NOT a "
            f"delivery failure")

    # 4. SCORE — local pass over this run's rows for the note/log; the
    # AUTHORITATIVE scores are rebuilt gateway-side over the full 30d window
    # at ingest (the runner is credential-free and holds no history).
    scores = probe.score(probes)
    flagged = [r for r in scores if probe.FLAG_NO_DELIVERY in r["flags"]]
    for r in flagged:
        log(f"⚠ {r['resource_url']}: {probe.FLAG_NO_DELIVERY} "
            f"({r['no_delivery_probes']}/{r['paid_probes']} paid probes)")
    for r in scores:
        if probe.FLAG_NO_DELIVERY_UNCONFIRMED in r["flags"]:
            log(f"· {r['resource_url']}: {probe.FLAG_NO_DELIVERY_UNCONFIRMED} "
                "— dropped from recommendations, re-probe queued")

    # 5. PUBLISH
    receipt = s.spending_summary()
    settled = sum(1 for p in paid_rows if p.get("settle_ok"))
    scoreable = sum(1 for p in paid_rows if not p.get("skipped"))
    by_outcome = {}
    for p in paid_rows:
        by_outcome[p.get("outcome") or "?"] = by_outcome.get(p.get("outcome") or "?", 0) + 1
    rejected = by_outcome.get("payment_rejected", 0)
    unreachable = by_outcome.get("unreachable", 0)
    unconfirmed = [r for r in scores
                   if probe.FLAG_NO_DELIVERY_UNCONFIRMED in r["flags"]]
    board = probe.need_leaderboard(scores)
    contested = {n: rows for n, rows in board.items() if len(rows) > 1}
    # The note is what a human skims on /ledger, so it has to carry the bad
    # news too — and label failures by WHOSE failure they were. "The seller
    # rejected the request after settlement" turned out to describe our own
    # bad params relayed back to us (AGE-86/AGE-87).
    note = (f"AgentPay prober — {run_at}\n"
            f"Probed {len(sel['t0'])} services ({scoreable} settled+scoreable "
            f"paid probes) across {len(needs)} needs; {t0_wf} well-formed 402s; "
            f"{len(flagged)} flagged {probe.FLAG_NO_DELIVERY}, "
            f"{len(unconfirmed)} unconfirmed")
    if rejected:
        note += (f"; {rejected} probe(s) transmitted a payment the seller "
                 f"rejected — settlement unconfirmed, excluded from delivery "
                 f"scores pending on-chain reconciliation")
    if unreachable:
        note += f"; {unreachable} unreachable pre-payment (never scored)"
    if contested:
        note += ("; head-to-head delivery on " +
                 ", ".join(f"{n} ({len(r)} providers)" for n, r in contested.items()))
    print("\n" + note + "\n", flush=True)
    print("PROBER_SWEEP " + json.dumps({
        "run_at": run_at, "goal": "probe_sweep", "note": note,
        "scores": scores, "receipt": receipt, "wallet": wallet.base_address,
    }, default=str), flush=True)
    log(f"run done | spent {receipt['spent']} of {receipt['budget']} "
        f"across {receipt['calls']} calls")

    # AGE-87: skipped rows ARE stored now — with `skipped`/`outcome`/`error`
    # columns, so an unscoreable probe is auditable from the database instead
    # of reconstructed from the receipt. score() excludes skipped rows from
    # every metric, and the gateway falls back to dropping them entirely if
    # the migration hasn't been applied (see insert_service_probes).
    ingest = publish_run(probes, {
        "run_at": run_at, "run_at_iso": run_at_iso, "wallet": wallet.base_address,
        "max_spend": str(max_spend),
        "objective": {"kind": "probe_sweep", "goal_text":
                      f"Probe {len(sel['t1'])} x402 services for delivery quality",
                      "needs": needs, "cap_usdc": str(max_spend)},
        "plan": {}, "regime": "", "context": "",
        "findings": {"probe_sweep": {
            "scores": scores,
            "t0": {"total": len(sel["t0"]), "alive": t0_alive,
                   "wellformed": t0_wf, "mpp_options": t0_mpp},
            # AGE-83: paid coverage is the differentiator, so it gets reported
            # as a first-class number instead of being inferred from `scores`.
            # AGE-86: `outcomes` breaks the attempts down by WHAT happened —
            # settled / payment_rejected / unreachable / settle_failed /
            # unfilled_path_template / cap_reached — so a bad sweep is
            # diagnosable from the ledger alone.
            "t1": {"attempted": len(sel["t1"]), "settled": settled,
                   "scoreable": scoreable, "outcomes": by_outcome,
                   "retested": len(retest),
                   "over_price_ceiling": len(sel["too_expensive"]),
                   "price_ceiling_usd": str(max_probe)},
            "need_leaderboard": board,
        }},
        "receipt": receipt, "note": note,
    })
    if ingest and ingest.get("scores"):
        window_flagged = [r for r in ingest["scores"]
                          if probe.FLAG_NO_DELIVERY in (r.get("flags") or [])]
        log(f"window scores: {len(ingest['scores'])} services, "
            f"{len(window_flagged)} flagged {probe.FLAG_NO_DELIVERY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
