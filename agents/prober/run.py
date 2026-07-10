#!/usr/bin/env python3
"""
run.py — the AgentPay Active Prober ("did it actually deliver?").

Delivery-quality telemetry for verified_route: spends pennies probing the
x402 marketplace as a REAL paying customer, then scores each service on
whether it delivered — the axis usage-ranking can't see (PROBER_SPEC).

Each run:
  1. SELECT  — canonical needs × rank() top-K + recent verified_route
               recommendations → deduped candidates, paid set capped
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


# ── Probes (I/O) ───────────────────────────────────────────────────────────────

def probe_free(cand: dict) -> dict:
    """T0: GET the resource, judge the live 402. Never pays."""
    status, body = None, None
    error = None
    try:
        req = urllib.request.Request(cand["url"], headers={"User-Agent": PROBE_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            status, body = r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:  # 402 arrives here
        status, body = e.code, e.read().decode(errors="replace")
    except Exception as e:
        error = str(e)[:200]
    checks = probe.t0_checks(status, body, cand.get("price_usd"))
    return _probe_row(cand, "free", error=error, **checks)


def probe_paid(session, cand: dict) -> dict:
    """T1: settle a real payment via the SDK, judge delivery. The Session cap
    is the only spend authority — would_exceed() gates before every call."""
    from agentpay import PaymentFailed, RefundPending

    price = cand.get("price_usd") or Decimal("0.01")
    if session.would_exceed(price):
        return _probe_row(cand, "paid", error="skipped: cap reached", skipped=True)

    settle_ok = http_ok = False
    latency_ms = None
    tx_hash = None
    error = None
    data = None
    t0 = time.monotonic()
    try:
        r = session.call(cand["url"])
        latency_ms = int((time.monotonic() - t0) * 1000)
        settle_ok = http_ok = True
        data = getattr(r, "data", r)
        tx_hash = getattr(r, "tx", None)
    except PaymentFailed as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        error = f"settle failed: {str(e)[:200]}"
    except RefundPending as e:
        # Payment settled but the tool failed to deliver → the worst outcome.
        latency_ms = int((time.monotonic() - t0) * 1000)
        settle_ok, http_ok = True, False
        error = f"paid, no delivery (refund pending): {str(e)[:200]}"
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        error = str(e)[:200]

    out_schema = (cand.get("accepts") or {}).get("outputSchema")
    checks = probe.t1_evaluate(data, out_schema) if http_ok else \
        {"response_nonempty": False, "schema_ok": None}
    return _probe_row(
        cand, "paid", error=error,
        settle_ok=settle_ok, http_ok=http_ok, latency_ms=latency_ms,
        tx_hash=tx_hash, **checks,
    )


def _probe_row(cand: dict, probe_type: str, **fields) -> dict:
    price = cand.get("price_usd")
    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "resource_url": cand["url"],
        "name": cand.get("name"),
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
            headers={"Content-Type": "application/json", "X-Flagship-Secret": secret},
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
    needs = _needs_from_env()

    # Fresh identity per run unless pinned — the funded key is Base-only.
    stellar_secret = os.environ.get("PROBER_STELLAR_SECRET", "") or Keypair.random().secret
    wallet = AgentWallet(secret_key=stellar_secret, network="mainnet", base_key=base_key)
    s = Session(wallet=wallet, gateway_url=GATEWAY, max_spend=max_spend)
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_at_iso = datetime.now(timezone.utc).isoformat()
    log(f"run start {run_at} | wallet {wallet.base_address} | cap ${max_spend} | "
        f"max paid probes {max_paid} | needs {len(needs)}")

    # 1. SELECT
    ranked = rank_needs(needs, Decimal(str(max_spend)))
    recent = recent_recommendations()
    sel = probe.select_candidates(ranked, recent, max_paid=max_paid)
    log(f"candidates: {len(sel['t0'])} total, {len(sel['t1'])} paid-eligible")

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

    # 3. T1 — paid probes, cap-gated
    paid_rows: list[dict] = []
    for cand in sel["t1"]:
        row = probe_paid(s, cand)
        paid_rows.append(row)
        if not row.get("skipped"):
            log(f"T1 {cand['name']}: settle={row.get('settle_ok')} "
                f"http={row.get('http_ok')} nonempty={row.get('response_nonempty')} "
                f"schema={row.get('schema_ok')} {row.get('latency_ms')}ms "
                f"| tx {row.get('tx_hash')}")
    probes.extend(paid_rows)

    # 4. SCORE — local pass over this run's rows for the note/log; the
    # AUTHORITATIVE scores are rebuilt gateway-side over the full 30d window
    # at ingest (the runner is credential-free and holds no history).
    scores = probe.score(probes)
    flagged = [r for r in scores if probe.FLAG_NO_DELIVERY in r["flags"]]
    for r in flagged:
        log(f"⚠ {r['resource_url']}: {probe.FLAG_NO_DELIVERY}")

    # 5. PUBLISH
    receipt = s.spending_summary()
    settled = sum(1 for p in paid_rows if p.get("settle_ok"))
    note = (f"AgentPay prober — {run_at}\n"
            f"Probed {len(sel['t0'])} services ({settled} paid settles) across "
            f"{len(needs)} needs; {t0_wf} well-formed 402s; "
            f"{len(flagged)} flagged {probe.FLAG_NO_DELIVERY}")
    print("\n" + note + "\n", flush=True)
    print("PROBER_SWEEP " + json.dumps({
        "run_at": run_at, "goal": "probe_sweep", "note": note,
        "scores": scores, "receipt": receipt, "wallet": wallet.base_address,
    }, default=str), flush=True)
    log(f"run done | spent {receipt['spent']} of {receipt['budget']} "
        f"across {receipt['calls']} calls")

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
