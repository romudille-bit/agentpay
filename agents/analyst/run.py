#!/usr/bin/env python3
"""
run.py — the AgentPay flagship analyst agent.

An autonomous market analyst that lives on AgentPay's own payment rails as a
REAL customer: it installs the published SDK (`pip install "agentpay-x402[base]"`),
talks to the production gateway over HTTP, holds its own funded wallet, and
operates under a hard per-run budget cap it cannot exceed.

Each run:
  1. PLAN     — decide what to look at (free intel + paid pre_trade_check on majors)
  2. ESTIMATE — price the whole plan via POST /v1/plan/estimate BEFORE spending
  3. EXECUTE  — free tools first, then buy verdicts while budget remains
  4. PUBLISH  — a market note + the spending receipt, as JSON on stdout
                (Railway logs are the v1 publish surface; /ledger reads
                payment_logs server-side)

Identity & config (env):
  FLAGSHIP_STELLAR_SECRET  — persistent Stellar secret (identity; unfunded is fine)
  FLAGSHIP_BASE_KEY        — persistent Base/EVM key (0x..; fund this with USDC)
  FLAGSHIP_MAX_SPEND       — hard cap per run in USDC (default "0.25")
  FLAGSHIP_SYMBOLS         — comma list for paid verdicts (default "BTC,ETH")
  AGENTPAY_GATEWAY_URL     — override gateway (default https://agentpay.tools)

Exit codes: 0 = note published; 1 = run failed (Railway cron surfaces it).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

GATEWAY = os.environ.get("AGENTPAY_GATEWAY_URL", "https://agentpay.tools")
TRADE_SIZE_USD = 25_000   # the notional the verdicts are priced at

# Production (Railway) pip-installs agentpay-x402; a local dev run from the
# repo has neither that nor the repo root on sys.path (python puts the
# SCRIPT's directory there, not the cwd). Fall back to the repo checkout.
try:
    import agentpay  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def log(msg: str) -> None:
    print(f"[analyst] {msg}", flush=True)


# ── Note composition (pure — unit-tested) ─────────────────────────────────────

def regime_line(fear_greed: dict | None, funding: dict | None) -> str:
    """One-line market regime from free intel. Defensive on missing data."""
    parts = []
    fg = (fear_greed or {}).get("value")
    fg_label = (fear_greed or {}).get("value_classification")
    if fg is not None:
        parts.append(f"Fear & Greed {fg} ({fg_label})")
    rates = (funding or {}).get("rates") or []
    if rates:
        bearish = sum(1 for r in rates if r.get("sentiment") == "bearish")
        bullish = sum(1 for r in rates if r.get("sentiment") == "bullish")
        if bearish > bullish:
            parts.append("funding leans crowded-long (bearish signal)")
        elif bullish > bearish:
            parts.append("funding leans crowded-short (bullish signal)")
        else:
            parts.append("funding unremarkable")
    return "; ".join(parts) if parts else "regime data unavailable"


def _human_usd(n: float | int | None) -> str | None:
    """Compact USD: 23_800_000_000 → '$23.8B'."""
    if not isinstance(n, (int, float)):
        return None
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"${n / div:.1f}{unit}"
    return f"${n:.0f}"


def news_summary(news: dict | None) -> dict | None:
    """Compress crypto_news headlines into count + net sentiment + top story."""
    headlines = (news or {}).get("headlines") or []
    if not headlines:
        return None
    bull = sum(1 for h in headlines if h.get("sentiment") == "bullish")
    bear = sum(1 for h in headlines if h.get("sentiment") == "bearish")
    net = "bullish" if bull > bear else "bearish" if bear > bull else "mixed"
    top = max(headlines, key=lambda h: h.get("score") or 0).get("title")
    return {"count": len(headlines), "net_sentiment": net,
            "bullish": bull, "bearish": bear, "top_headline": top}


def _tvl_total(tvl: dict | list | None) -> float | None:
    """Total DeFi TVL from defi_tvl, tolerant of single-protocol or top-N shapes."""
    if isinstance(tvl, dict):
        if isinstance(tvl.get("tvl"), (int, float)):
            return float(tvl["tvl"])
        items = tvl.get("protocols")
        if isinstance(items, list):
            return float(sum(p.get("tvl", 0) for p in items if isinstance(p, dict)))
    if isinstance(tvl, list):
        return float(sum(p.get("tvl", 0) for p in tvl if isinstance(p, dict)))
    return None


def context_line(news: dict | None, gas: dict | None, tvl: dict | list | None) -> str:
    """One-line macro context from the curated free tools (defensive on missing)."""
    parts: list[str] = []
    ns = news_summary(news)
    if ns:
        parts.append(f"{ns['count']} headlines (net {ns['net_sentiment']})")
    if gas and gas.get("standard_gwei") is not None:
        parts.append(f"ETH gas {gas['standard_gwei']} gwei")
    total = _tvl_total(tvl)
    if total is not None:
        ch = (tvl or {}).get("change_1d") if isinstance(tvl, dict) else None
        chs = f" ({ch:+.1f}% 24h)" if isinstance(ch, (int, float)) else ""
        parts.append(f"DeFi TVL {_human_usd(total)}{chs}")
    return "; ".join(parts)


def compose_note(
    run_at: str,
    regime: str,
    verdicts: dict[str, dict],
    skipped: dict[str, str],
    context: str = "",
) -> str:
    """Render the daily market note. `verdicts` maps symbol → pre_trade_check
    data; `skipped` maps symbol → reason; `context` is the optional free-intel
    macro line (news/gas/DeFi)."""
    lines = [
        f"AgentPay flagship analyst — {run_at}",
        f"Regime: {regime}",
    ]
    if context:
        lines.append(f"Context: {context}")
    lines += [
        "",
        f"Long-entry check at ${TRADE_SIZE_USD:,} notional:",
    ]
    for sym, v in verdicts.items():
        factors = v.get("factors", {})
        worst = [
            f"{name}: {f.get('reason', '?')}"
            for name, f in factors.items()
            if f.get("level") in ("caution", "avoid")
        ]
        detail = f" ({'; '.join(worst)})" if worst else ""
        lines.append(f"  {sym}: {v.get('verdict', '?').upper()}{detail}")
    for sym, why in skipped.items():
        lines.append(f"  {sym}: skipped ({why})")
    return "\n".join(lines)


# ── The run ───────────────────────────────────────────────────────────────────

def main() -> int:
    from agentpay import AgentWallet, Session, PaymentFailed, RefundPending

    stellar_secret = os.environ.get("FLAGSHIP_STELLAR_SECRET", "")
    base_key       = os.environ.get("FLAGSHIP_BASE_KEY", "")
    if not (stellar_secret and base_key):
        log("FATAL: FLAGSHIP_STELLAR_SECRET and FLAGSHIP_BASE_KEY are required")
        return 1

    max_spend = os.environ.get("FLAGSHIP_MAX_SPEND", "0.25")
    symbols = [s.strip().upper() for s in
               os.environ.get("FLAGSHIP_SYMBOLS", "BTC,ETH").split(",") if s.strip()]
    # The run's stated goal, captured before any budget trim mutates `symbols`.
    objective = {
        "symbols":        list(symbols),
        "trade_size_usd": TRADE_SIZE_USD,
        "side":           "long",
        "cap_usdc":       str(max_spend),
    }

    wallet = AgentWallet(secret_key=stellar_secret, network="mainnet", base_key=base_key)
    s = Session(wallet=wallet, gateway_url=GATEWAY, max_spend=max_spend)
    run_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_at_iso = datetime.now(tz=timezone.utc).isoformat()
    log(f"run start {run_at} | wallet {wallet.base_address} | cap ${max_spend}")

    # Curated free-intel set: regime (fear/greed + funding), macro snapshot, plus
    # catalysts (news), network demand (gas), and the DeFi landscape (TVL). All
    # $0 — they never settle on-chain — but they make the note materially richer
    # and exercise more of the free catalog. News currencies track the original
    # symbol list (before any budget trim of the paid verdicts).
    news_currencies = ",".join(symbols) or "BTC,ETH"
    FREE_TOOLS = [
        ("fear_greed_index", {}),
        ("funding_rates", {}),
        ("market_snapshot", {}),
        ("crypto_news", {"currencies": news_currencies}),
        ("gas_tracker", {}),
        ("defi_tvl", {}),
    ]
    free_step_names = [t for t, _ in FREE_TOOLS]

    # 1-2. PLAN + ESTIMATE — price everything before spending a cent
    plan_steps = free_step_names + ["pre_trade_check"] * len(symbols)
    plan = s.estimate_plan(plan_steps)
    log(f"plan: total ${plan['total_usdc']} for {len(plan_steps)} steps | "
        f"fits budget: {plan.get('fits_budget')}")
    if plan.get("fits_budget") is False:
        # Trim paid steps until the plan fits — the cap is the law. Free intel
        # is never trimmed (it costs nothing against the cap).
        while symbols and plan.get("fits_budget") is False:
            symbols.pop()
            plan = s.estimate_plan(free_step_names + ["pre_trade_check"] * len(symbols))
        log(f"plan trimmed to fit: {len(symbols)} paid verdicts")

    # 3. EXECUTE — free intel first (these cost $0 and never settle on-chain)
    intel: dict[str, dict | None] = {}
    free_used: list[str] = []
    for tool, params in FREE_TOOLS:
        try:
            intel[tool] = s.call(tool, params).data
            free_used.append(tool)
        except Exception as e:
            log(f"free intel {tool} failed: {e}")
            intel[tool] = None

    # Paid verdicts — stop the moment the cap says stop
    verdicts: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    for sym in symbols:
        if s.would_exceed(s.tool_cost_usd("pre_trade_check") or Decimal("0.01")):
            skipped[sym] = "budget cap reached"
            continue
        try:
            r = s.call("pre_trade_check",
                       {"symbol": sym, "size_usd": TRADE_SIZE_USD, "side": "long"})
            verdicts[sym] = r.data
            log(f"bought verdict {sym}: {r.data.get('verdict')} | tx {r.tx}")
        except (PaymentFailed, RefundPending) as e:
            log(f"paid verdict {sym} failed: {e}")
            skipped[sym] = "payment failed"

    # 4. PUBLISH — note + receipt as structured stdout
    context = context_line(intel.get("crypto_news"), intel.get("gas_tracker"),
                           intel.get("defi_tvl"))
    note = compose_note(
        run_at,
        regime_line(intel.get("fear_greed_index"), intel.get("funding_rates")),
        verdicts, skipped, context=context,
    )
    receipt = s.spending_summary()
    free_intel = {
        "tools": free_used,
        "news": news_summary(intel.get("crypto_news")),
        "gas_gwei": (intel.get("gas_tracker") or {}).get("standard_gwei"),
        "defi_tvl_usd": _tvl_total(intel.get("defi_tvl")),
    }
    print("\n" + note + "\n", flush=True)
    print("FLAGSHIP_NOTE " + json.dumps({
        "run_at": run_at,
        "note": note,
        "verdicts": {k: v.get("verdict") for k, v in verdicts.items()},
        "receipt": receipt,
        "wallet": wallet.base_address,
    }), flush=True)
    log(f"run done | spent {receipt['spent']} of {receipt['budget']} "
        f"across {receipt['calls']} calls")

    # 5. PERSIST — POST the full run (plan + reasoning + receipt) to the gateway
    # so /ledger can show *why* each call happened, not just that it did. Best-
    # effort: a failure here never affects the run's exit code (the spend already
    # happened and is on-chain regardless).
    publish_run({
        "run_at": run_at,
        "run_at_iso": run_at_iso,
        "wallet": wallet.base_address,
        "max_spend": str(max_spend),
        "objective": objective,
        "plan": plan,
        "regime": regime_line(intel.get("fear_greed_index"), intel.get("funding_rates")),
        "context": context,
        "verdicts": verdicts,
        "skipped": skipped,
        "receipt": receipt,
        "free_intel": free_intel,
        "note": note,
    })

    # A run that produced no verdicts at all is a failure worth surfacing.
    return 0 if verdicts or not symbols else 1


def publish_run(payload: dict) -> bool:
    """POST a completed run to the gateway's flagship ingest endpoint.

    Auth is a shared secret (FLAGSHIP_INGEST_SECRET) sent as a header; the gateway
    holds the Supabase credentials and does the write, so the flagship stays a
    credential-free HTTP customer. No-op (returns False) when the secret is unset.
    Best-effort: any failure is logged and swallowed.
    """
    secret = os.environ.get("FLAGSHIP_INGEST_SECRET", "")
    if not secret:
        log("ingest skipped — FLAGSHIP_INGEST_SECRET unset")
        return False
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{GATEWAY}/v1/flagship/run",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Flagship-Secret": secret},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
        log(f"ingest {'ok' if ok else 'failed'} → /v1/flagship/run")
        return ok
    except Exception as e:
        log(f"ingest failed: {e}")
        return False


if __name__ == "__main__":
    sys.exit(main())
