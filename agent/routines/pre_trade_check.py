"""
pre_trade_check.py — AgentPay market context check for Condor agents.
======================================================================

WHAT THIS DOES
--------------
Runs once at the start of every Condor tick, before the LLM makes a
trade decision. Pulls 4 real-time signals and returns a structured
verdict: PROCEED, CAUTION, or ABORT.

The LLM gets the verdict + reasoning string and can use it to:
  - Gate entry (don't open a new position if verdict is ABORT)
  - Size down (reduce position size if verdict is CAUTION)
  - Confirm conviction (PROCEED + supporting context)

This is NOT strategy-specific. It works with any Condor Trading Agent —
momentum, mean-reversion, carry, or anything else. It answers:
  "Is it safe to trade right now, and what's the market mood?"

SIGNALS (~$0.008 per tick)
--------------------------
  funding_rates    ($0.003) — market regime: are longs or shorts paying?
  open_interest    ($0.002) — conviction: is money flowing in or out?
  fear_greed_index ($0.001) — sentiment: crowding risk?
  whale_activity   ($0.002) — tail risk: large moves imminent?

VERDICT LOGIC
-------------
  PROCEED  — all clear. Normal conditions, no red flags.
  CAUTION  — one concern. Trade if your edge is strong; consider smaller size.
  ABORT    — multiple red flags. Skip this tick.

DROP-IN USAGE (Condor)
----------------------
1. Copy this file to: ~/condor/agents/your-agent/pre_trade_check.py
2. In your agent's tick logic, call it at the start:

    from pre_trade_check import run as pre_trade_check

    def on_tick(context):
        check = pre_trade_check(context)
        if check["verdict"] == "abort":
            return  # skip this tick
        # ... rest of your strategy

3. Set STELLAR_SECRET_KEY (or TEST_AGENT_SECRET_KEY for testnet) in env.
4. Optional: set AGENTPAY_NETWORK=testnet for paper trading.

The full check dict is passed into Condor's LLM context as structured data
so it can reason about the verdict and override if needed.

CONTEXT KEYS (optional, passed in by Condor)
--------------------------------------------
  context["strategy"]        (str)  — e.g. "momentum", "carry", "arb"
  context["proposed_action"] (str)  — what the agent is about to do
  context["position_size_usd"] (float) — helps calibrate whale threshold

ENVIRONMENT VARIABLES
---------------------
  AGENTPAY_NETWORK          mainnet | testnet (default: mainnet)
  AGENTPAY_GATEWAY_URL      Override gateway URL
  PRETRADE_ASSET            Asset to check (default: ETH)
  PRETRADE_BUDGET           Max AgentPay spend per tick (default: $0.02)
  PRETRADE_WHALE_ABORT_USD  Abort if whale vol exceeds this (default: 5000000)
  PRETRADE_WHALE_CAUTION_USD  Caution if whale vol exceeds this (default: 1000000)
  PRETRADE_FG_EXTREME_HIGH  Abort if F&G above this (default: 90)
  PRETRADE_FG_EXTREME_LOW   Abort if F&G below this (default: 10)
  PRETRADE_OI_DROP_ABORT    Abort if OI 24h change below this % (default: -15)
  PRETRADE_OI_DROP_CAUTION  Caution if OI 24h change below this % (default: -8)
"""

import os
import sys
import logging
from datetime import datetime, timezone

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "../.."))

from agent.wallet import AgentWallet, Session, BudgetExceeded

logger = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────────

_DEFAULT_GATEWAYS = {
    "mainnet": "https://gateway-production-2cc2.up.railway.app",
    "testnet": "https://gateway-testnet-production.up.railway.app",
}

NETWORK    = os.environ.get("AGENTPAY_NETWORK",      "mainnet")
GATEWAY    = os.environ.get("AGENTPAY_GATEWAY_URL",  _DEFAULT_GATEWAYS[NETWORK])
ASSET      = os.environ.get("PRETRADE_ASSET",         "ETH")
MAX_BUDGET = os.environ.get("PRETRADE_BUDGET",        "0.02")

WHALE_ABORT_USD   = float(os.environ.get("PRETRADE_WHALE_ABORT_USD",   "5000000"))
WHALE_CAUTION_USD = float(os.environ.get("PRETRADE_WHALE_CAUTION_USD", "1000000"))
FG_EXTREME_HIGH   = int(os.environ.get("PRETRADE_FG_EXTREME_HIGH",     "90"))
FG_EXTREME_LOW    = int(os.environ.get("PRETRADE_FG_EXTREME_LOW",      "10"))
OI_DROP_ABORT     = float(os.environ.get("PRETRADE_OI_DROP_ABORT",     "-15.0"))
OI_DROP_CAUTION   = float(os.environ.get("PRETRADE_OI_DROP_CAUTION",   "-8.0"))


# ── Main entry point ───────────────────────────────────────────────────────────

def run(context: dict = None) -> dict:
    """
    Call this at the start of every Condor tick.

    Returns:
        {
            "verdict":    str  — "proceed" | "caution" | "abort"
            "risk_level": str  — "low" | "medium" | "high"
            "flags":      list — list of active warning strings
            "signals":    dict — all raw signal values
            "reasoning":  str  — plain English summary for the LLM
            "cost":       str  — AgentPay spend this tick (e.g. "$0.008")
            "timestamp":  str  — UTC ISO timestamp
        }
    """
    if context is None:
        context = {}

    secret = (
        os.environ.get("STELLAR_SECRET_KEY") or
        (os.environ.get("TEST_AGENT_SECRET_KEY") if NETWORK == "testnet" else None) or
        ""
    )

    if not secret:
        return _error_result("No secret key — set STELLAR_SECRET_KEY or TEST_AGENT_SECRET_KEY")

    wallet  = AgentWallet(secret_key=secret, network=NETWORK)
    signals = {}

    try:
        with Session(wallet, gateway_url=GATEWAY, max_spend=MAX_BUDGET) as session:
            signals = _gather_signals(session)
    except BudgetExceeded as e:
        logger.warning(f"[pre_trade] Budget exceeded: {e}")
        return _error_result(f"Signal budget exceeded: {e}")
    except Exception as e:
        logger.error(f"[pre_trade] Signal gathering failed: {e}")
        return _error_result(str(e))

    verdict, risk_level, flags = _evaluate(signals, context)
    reasoning                  = _explain(signals, verdict, flags, context)

    logger.info(f"[pre_trade] {verdict.upper()} ({risk_level}) — {reasoning[:120]}")

    return {
        "verdict":    verdict,
        "risk_level": risk_level,
        "flags":      flags,
        "signals":    signals,
        "reasoning":  reasoning,
        "cost":       signals.get("cost", "?"),
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }


# ── Signal gathering ───────────────────────────────────────────────────────────

def _gather_signals(session) -> dict:
    """
    Gather 4 signals. ~$0.008 total.
    """
    signals = {}

    # 1. Funding rates — market regime
    rates_raw = session.call("funding_rates", {"asset": ASSET})
    rates     = rates_raw.get("result", rates_raw)
    exchanges = rates.get("rates", rates.get("exchanges", []))

    if exchanges:
        avg_rate = sum(e["funding_rate_pct"] for e in exchanges) / len(exchanges)
        max_rate = max(e["funding_rate_pct"] for e in exchanges)
        min_rate = min(e["funding_rate_pct"] for e in exchanges)
        signals["funding_avg_pct"]      = avg_rate
        signals["funding_max_pct"]      = max_rate
        signals["funding_min_pct"]      = min_rate
        signals["funding_annualized"]   = avg_rate * 3 * 365
        signals["funding_regime"]       = (
            "positive" if avg_rate > 0.005 else
            "negative" if avg_rate < -0.005 else
            "neutral"
        )
        signals["funding_data_ok"]      = True
    else:
        signals["funding_avg_pct"]      = 0.0
        signals["funding_max_pct"]      = 0.0
        signals["funding_min_pct"]      = 0.0
        signals["funding_annualized"]   = 0.0
        signals["funding_regime"]       = "unknown"
        signals["funding_data_ok"]      = False

    # 2. Open interest — market conviction
    oi_raw = session.call("open_interest", {"symbol": ASSET})
    oi     = oi_raw.get("result", oi_raw)
    signals["oi_change_24h_pct"]  = oi.get("oi_change_24h_pct", 0)
    signals["oi_change_1h_pct"]   = oi.get("oi_change_1h_pct",  0)
    signals["long_short_ratio"]   = oi.get("long_short_ratio",  1.0)
    signals["oi_momentum"]        = (
        "expanding"   if signals["oi_change_24h_pct"] >  5 else
        "contracting" if signals["oi_change_24h_pct"] < -5 else
        "flat"
    )

    # 3. Fear & Greed — sentiment / crowding
    fg_raw = session.call("fear_greed_index", {})
    fg     = fg_raw.get("result", fg_raw)
    fg_val = fg.get("value", 50)
    signals["fear_greed_value"]   = fg_val
    signals["fear_greed_label"]   = fg.get("value_classification", "Neutral")
    signals["sentiment_extreme"]  = fg_val >= FG_EXTREME_HIGH or fg_val <= FG_EXTREME_LOW

    # 4. Whale activity — tail risk
    whale_raw = session.call("whale_activity", {"token": ASSET, "min_usd": 500000})
    whales    = whale_raw.get("result", whale_raw)
    whale_vol = whales.get("total_volume_usd", 0)
    signals["whale_volume_usd"]     = whale_vol
    signals["whale_transfer_count"] = len(whales.get("large_transfers", []))
    signals["whale_risk"]           = (
        "high"   if whale_vol >= WHALE_ABORT_USD   else
        "medium" if whale_vol >= WHALE_CAUTION_USD else
        "low"
    )

    signals["cost"] = session.spent()
    return signals


# ── Evaluation ─────────────────────────────────────────────────────────────────

def _evaluate(signals: dict, context: dict) -> tuple:
    """
    Returns (verdict, risk_level, flags).

    Rules:
      - Any ABORT condition → "abort", "high"
      - 2+ CAUTION conditions → "abort", "high"
      - 1 CAUTION condition → "caution", "medium"
      - No flags → "proceed", "low"
    """
    abort_flags   = []
    caution_flags = []

    fg        = signals.get("fear_greed_value",   50)
    whale_vol = signals.get("whale_volume_usd",    0)
    oi_24h    = signals.get("oi_change_24h_pct",   0)
    ls        = signals.get("long_short_ratio",    1.0)

    # ── Hard aborts ───────────────────────────────────────────────────────────
    if whale_vol >= WHALE_ABORT_USD:
        abort_flags.append(
            f"Large whale movement detected: ${whale_vol:,.0f} — potential market disruption"
        )
    if fg >= FG_EXTREME_HIGH:
        abort_flags.append(
            f"Extreme greed ({fg}/100) — market may be overextended, reversal risk"
        )
    if fg <= FG_EXTREME_LOW:
        abort_flags.append(
            f"Extreme fear ({fg}/100) — capitulation risk, high volatility likely"
        )
    if oi_24h <= OI_DROP_ABORT:
        abort_flags.append(
            f"OI collapsed {oi_24h:.1f}% in 24h — large positions unwinding, liquidity thinning"
        )

    # ── Caution flags ─────────────────────────────────────────────────────────
    if WHALE_CAUTION_USD <= whale_vol < WHALE_ABORT_USD:
        caution_flags.append(
            f"Elevated whale activity: ${whale_vol:,.0f} — monitor for follow-through"
        )
    if OI_DROP_ABORT < oi_24h <= OI_DROP_CAUTION:
        caution_flags.append(
            f"OI declining {oi_24h:.1f}%/24h — conviction softening"
        )
    if (fg >= FG_EXTREME_HIGH - 10) and fg < FG_EXTREME_HIGH:
        caution_flags.append(
            f"Fear/Greed approaching extreme ({fg}/100) — watch for reversal"
        )
    if (fg <= FG_EXTREME_LOW + 10) and fg > FG_EXTREME_LOW:
        caution_flags.append(
            f"Fear/Greed approaching extreme fear ({fg}/100) — elevated volatility"
        )
    if not signals.get("funding_data_ok", True):
        caution_flags.append(
            "Funding rate data unavailable this tick — decision quality reduced"
        )

    # ── Verdict ───────────────────────────────────────────────────────────────
    if abort_flags:
        return "abort", "high", abort_flags + caution_flags
    if len(caution_flags) >= 2:
        return "abort", "high", caution_flags
    if len(caution_flags) == 1:
        return "caution", "medium", caution_flags
    return "proceed", "low", []


# ── Explanation ────────────────────────────────────────────────────────────────

def _explain(signals: dict, verdict: str, flags: list, context: dict) -> str:
    fg       = signals.get("fear_greed_value",   50)
    fg_label = signals.get("fear_greed_label",   "Neutral")
    funding  = signals.get("funding_avg_pct",     0)
    annual   = signals.get("funding_annualized",  0)
    regime   = signals.get("funding_regime",      "neutral")
    oi_24h   = signals.get("oi_change_24h_pct",   0)
    ls       = signals.get("long_short_ratio",    1.0)
    whale    = signals.get("whale_volume_usd",     0)
    oi_mom   = signals.get("oi_momentum",         "flat")
    strategy = context.get("strategy",            "")

    market_summary = (
        f"Funding {funding:+.4f}%/8h ({regime}, ~{annual:.0f}% annualized). "
        f"OI {oi_24h:+.1f}%/24h ({oi_mom}), L/S {ls:.2f}. "
        f"Sentiment: {fg} ({fg_label}). Whale vol: ${whale:,.0f}."
    )

    if verdict == "proceed":
        return (
            f"PROCEED — Market conditions normal{' for ' + strategy if strategy else ''}. "
            f"{market_summary}"
        )

    if verdict == "caution":
        flag_str = " | ".join(flags)
        return (
            f"CAUTION — Trade with reduced size or tighter stops. "
            f"{flag_str}. {market_summary}"
        )

    # abort
    flag_str = " | ".join(flags)
    return (
        f"ABORT — Skip this tick. "
        f"{flag_str}. {market_summary}"
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _error_result(msg: str) -> dict:
    return {
        "verdict":    "abort",
        "risk_level": "unknown",
        "flags":      [f"Signal error: {msg}"],
        "signals":    {},
        "reasoning":  f"ABORT — Could not gather signals: {msg}",
        "cost":       "$0.000",
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    from dotenv import load_dotenv

    _env_path = os.path.join(_here, "../..", ".env")
    load_dotenv(_env_path)

    # CLI flags
    verbose  = "--verbose"  in sys.argv
    testnet  = "--testnet"  in sys.argv

    if testnet:
        import agent.routines.pre_trade_check as _self
        _self.NETWORK = "testnet"
        _self.GATEWAY = _DEFAULT_GATEWAYS["testnet"]

    context = {
        "strategy":          os.environ.get("PRETRADE_STRATEGY", ""),
        "proposed_action":   os.environ.get("PRETRADE_ACTION",   "enter"),
        "position_size_usd": float(os.environ.get("PRETRADE_SIZE", "1000")),
    }

    print(f"\n{'═'*62}")
    print(f"  AgentPay — Pre-Trade Check")
    print(f"  Asset: {ASSET} | Network: {NETWORK}")
    if context["strategy"]:
        print(f"  Strategy: {context['strategy']}")
    print(f"{'═'*62}\n")

    result = run(context=context)

    verdict    = result["verdict"].upper()
    risk       = result["risk_level"].upper()
    verdict_icon = {"PROCEED": "✅", "CAUTION": "⚠️ ", "ABORT": "🛑"}.get(verdict, "?")

    print(f"  {verdict_icon}  Verdict:    {verdict}")
    print(f"      Risk:       {risk}")
    print(f"      Cost:       {result['cost']}")
    print()
    print(f"  Reasoning: {result['reasoning']}")
    print()

    if result["flags"]:
        print("  Flags:")
        for f in result["flags"]:
            print(f"    • {f}")
        print()

    if verbose:
        print("  Raw signals:")
        for k, v in result.get("signals", {}).items():
            if k != "cost":
                print(f"    {k:<30} {v}")
    print()
