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


def compose_note(
    run_at: str,
    regime: str,
    verdicts: dict[str, dict],
    skipped: dict[str, str],
) -> str:
    """Render the daily market note. `verdicts` maps symbol → pre_trade_check
    data; `skipped` maps symbol → reason."""
    lines = [
        f"AgentPay flagship analyst — {run_at}",
        f"Regime: {regime}",
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

    wallet = AgentWallet(secret_key=stellar_secret, network="mainnet", base_key=base_key)
    s = Session(wallet=wallet, gateway_url=GATEWAY, max_spend=max_spend)
    run_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log(f"run start {run_at} | wallet {wallet.base_address} | cap ${max_spend}")

    # 1-2. PLAN + ESTIMATE — price everything before spending a cent
    plan_steps = ["fear_greed_index", "funding_rates", "market_snapshot"] + \
                 ["pre_trade_check"] * len(symbols)
    plan = s.estimate_plan(plan_steps)
    log(f"plan: total ${plan['total_usdc']} for {len(plan_steps)} steps | "
        f"fits budget: {plan.get('fits_budget')}")
    if plan.get("fits_budget") is False:
        # Trim paid steps until the plan fits — the cap is the law.
        while symbols and plan.get("fits_budget") is False:
            symbols.pop()
            plan = s.estimate_plan(
                ["fear_greed_index", "funding_rates", "market_snapshot"]
                + ["pre_trade_check"] * len(symbols))
        log(f"plan trimmed to fit: {len(symbols)} paid verdicts")

    # 3. EXECUTE — free intel first (these cost $0 and never settle on-chain)
    intel: dict[str, dict | None] = {}
    for tool, params in (("fear_greed_index", {}), ("funding_rates", {}),
                         ("market_snapshot", {})):
        try:
            intel[tool] = s.call(tool, params).data
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
    note = compose_note(
        run_at,
        regime_line(intel.get("fear_greed_index"), intel.get("funding_rates")),
        verdicts, skipped,
    )
    receipt = s.spending_summary()
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

    # A run that produced no verdicts at all is a failure worth surfacing.
    return 0 if verdicts or not symbols else 1


if __name__ == "__main__":
    sys.exit(main())
