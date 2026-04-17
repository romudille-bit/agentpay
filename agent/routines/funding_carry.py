"""
Funding Rate Carry Routine for Condor
======================================

WHAT THIS STRATEGY DOES
------------------------
A funding rate carry trade is delta-neutral — you hold two opposing positions at once
so that price moves cancel out, and you only collect the funding rate.

There are TWO directions, and this routine handles both:

  LONG CARRY (bullish markets, positive funding)
  ------------------------------------------------
  - Long ETH spot + Short ETH-PERP
  - Longs pay shorts every 8h → you collect as the short
  - Enter when: funding is significantly positive, sentiment is greed but not extreme,
    OI is rising (fresh long demand), no large whale moves

  SHORT CARRY (bearish markets, negative funding)
  ------------------------------------------------
  - Short ETH spot + Long ETH-PERP
  - Shorts pay longs every 8h → you collect as the long
  - Enter when: funding is significantly negative, sentiment is fear but not extreme,
    OI is rising (fresh short demand), no large whale moves

  Example (long carry):
    Position: $10,000 ETH spot + $10,000 ETH-PERP short (delta neutral)
    Funding:  +0.05%/8h avg
    Payment:  $10,000 × 0.05% = $5 every 8h = $15/day = ~54% APY
    Signal cost: $0.008 USDC/tick × 3 ticks/day = $0.024/day

WHY THE SAME 4 SIGNALS WORK FOR BOTH DIRECTIONS
-------------------------------------------------
  1. funding_rates
       Positive → long carry opportunity
       Negative → short carry opportunity
       Near zero → skip, nothing to collect

  2. open_interest
       Rising OI confirms conviction in the dominant direction:
         - Positive funding + rising OI = longs piling in = rate stays positive
         - Negative funding + rising OI = shorts piling in = rate stays negative
       Falling OI = dominant side unwinding = rate moving back to zero → skip

  3. fear_greed_index
       Acts as a crowding check — symmetric in both directions:
         - Extreme greed (>80): long carry is crowded, sentiment flip risk
         - Extreme fear (<20): short carry is crowded, sentiment flip risk
       Sweet spot: enter long carry in greed (50–79), short carry in fear (21–49)

  4. whale_activity
       Large transfers near a funding window = tail risk in either direction.
       Someone moving $5M+ could close a large position, moving the rate and
       the price simultaneously. Skip or exit regardless of direction.

HOW CONDOR USES THIS
---------------------
Condor calls run() on a schedule (recommended: every 4h, before each funding window).
run() returns: { action, direction, asset, signals, reasoning, cost }

Condor's LLM reads the reasoning string and can override. Hummingbot executes:

  enter_long_carry:
    BUY {ASSET} spot (binance_spot) + SELL {ASSET}-PERP (binance_perpetual)

  enter_short_carry:
    SELL {ASSET} spot (binance_spot) + BUY {ASSET}-PERP (binance_perpetual)

  exit_long_carry / exit_short_carry:
    Close both legs simultaneously

  hold / skip:
    No action

CONTEXT KEYS (passed in by Condor)
------------------------------------
  context["carry_position"]   (bool)  — are we currently in a carry position?
  context["carry_direction"]  (str)   — "long" | "short" | None
  context["position_size_usd"] (float) — current position size in USD

PARAMETERS (set via environment variables)
------------------------------------------
  AGENTPAY_GATEWAY_URL      Gateway URL (default: production mainnet)
  CARRY_ASSET               Asset to trade (default: ETH)
  CARRY_BUDGET_PER_TICK     Max AgentPay spend per call (default: $0.02)
  FUNDING_ENTER_THRESHOLD   Min |funding rate| to enter, in %/8h (default: 0.01)
  FUNDING_EXIT_THRESHOLD    Exit when |funding| drops below this (default: 0.002)
  MAX_WHALE_VOL_USD         Abort/exit if whale volume exceeds this (default: 5000000)
  FEAR_GREED_GREED_MIN      Min F&G to enter long carry (default: 50)
  FEAR_GREED_GREED_MAX      Max F&G to enter long carry (default: 79)
  FEAR_GREED_FEAR_MIN       Min F&G to enter short carry (default: 21)
  FEAR_GREED_FEAR_MAX       Max F&G to enter short carry (default: 49)
  OI_MIN_CHANGE             Skip if OI 24h change is below this % (default: -5.0)
"""

import os
import sys
import logging

# Add project root to path so `agent.wallet` resolves correctly
# whether this is run directly or imported by Condor
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "../.."))

from agent.wallet import AgentWallet, Session, BudgetExceeded

logger = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────────

_DEFAULT_GATEWAYS = {
    "mainnet": "https://gateway-production-2cc2.up.railway.app",
    "testnet": "https://gateway-testnet-production.up.railway.app",
}
NETWORK    = os.environ.get("AGENTPAY_NETWORK", "mainnet")
GATEWAY    = os.environ.get("AGENTPAY_GATEWAY_URL", _DEFAULT_GATEWAYS[NETWORK])
ASSET      = os.environ.get("CARRY_ASSET",           "ETH")
MAX_BUDGET = os.environ.get("CARRY_BUDGET_PER_TICK", "0.02")

FUNDING_ENTER_THRESHOLD = float(os.environ.get("FUNDING_ENTER_THRESHOLD", "0.01"))    # %/8h min |rate| to enter
FUNDING_EXIT_THRESHOLD  = float(os.environ.get("FUNDING_EXIT_THRESHOLD",  "0.002"))   # exit when |rate| falls below
MAX_WHALE_VOL_USD        = float(os.environ.get("MAX_WHALE_VOL_USD",       "5000000"))
OI_MIN_CHANGE            = float(os.environ.get("OI_MIN_CHANGE",           "-5.0"))    # %/24h floor

# Fear & Greed bands for each carry direction
# Long carry: enter in greed zone (not extreme greed)
# Short carry: enter in fear zone (not extreme fear)
FG_LONG_MIN  = int(os.environ.get("FEAR_GREED_GREED_MIN", "50"))   # too neutral below this for long carry
FG_LONG_MAX  = int(os.environ.get("FEAR_GREED_GREED_MAX", "79"))   # extreme greed above this = crowded
FG_SHORT_MIN = int(os.environ.get("FEAR_GREED_FEAR_MIN",  "21"))   # extreme fear below this = crowded
FG_SHORT_MAX = int(os.environ.get("FEAR_GREED_FEAR_MAX",  "49"))   # too neutral above this for short carry


# ── Main entry point ───────────────────────────────────────────────────────────

def run(context: dict = None) -> dict:
    """
    Condor calls this function on every tick.

    Returns:
        {
            "action":    str  — "enter_long_carry" | "enter_short_carry" |
                                "exit_long_carry"  | "exit_short_carry"  |
                                "hold" | "skip" | "error"
            "direction": str  — "long" | "short" | None
            "asset":     str  — e.g. "ETH"
            "signals":   dict — all raw signal values
            "reasoning": str  — plain English explanation
            "cost":      str  — AgentPay spend this tick
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
        return {
            "action":    "error",
            "direction": None,
            "reasoning": "No secret key found — set STELLAR_SECRET_KEY or TEST_AGENT_SECRET_KEY",
            "signals":   {},
            "cost":      "$0.000",
        }

    wallet  = AgentWallet(secret_key=secret, network=NETWORK)
    signals = {}

    try:
        with Session(wallet, gateway_url=GATEWAY, max_spend=MAX_BUDGET) as session:
            signals = _gather_signals(session)
    except BudgetExceeded as e:
        logger.warning(f"[carry] Budget exceeded: {e}")
        return {"action": "skip", "direction": None, "reasoning": f"Signal budget exceeded: {e}", "signals": signals, "cost": "?"}
    except Exception as e:
        logger.error(f"[carry] Signal gathering failed: {e}")
        return {"action": "error", "direction": None, "reasoning": str(e), "signals": signals, "cost": "?"}

    action, direction = _decide(signals, context)
    reasoning         = _explain(signals, action, direction)

    logger.info(f"[carry] {action.upper()} — {reasoning}")

    return {
        "action":    action,
        "direction": direction,
        "lean":      signals.get("lean", "neutral"),   # directional bias even when skipping
        "asset":     ASSET,
        "signals":   signals,
        "reasoning": reasoning,
        "cost":      signals.get("cost", "?"),
    }


# ── Signal gathering ───────────────────────────────────────────────────────────

def _gather_signals(session: "Session") -> dict:
    """
    Call 4 AgentPay tools and return a flat signal dict.
    ~$0.008 USDC total (funding_rates $0.003 + open_interest $0.002
                        + fear_greed $0.001 + whale_activity $0.002)
    """
    signals = {}

    # 1. Funding rates — direction + magnitude
    # Positive avg = long carry opportunity
    # Negative avg = short carry opportunity
    # Near zero = nothing to collect, skip
    rates_raw = session.call("funding_rates", {"asset": ASSET})
    rates     = rates_raw.get("result", rates_raw)
    exchanges = rates.get("rates", rates.get("exchanges", []))  # gateway returns "rates"

    if exchanges:
        avg_rate = sum(e["funding_rate_pct"] for e in exchanges) / len(exchanges)
        # Best exchange for long carry = highest rate (most to collect as short)
        # Best exchange for short carry = lowest rate (most to collect as long)
        best_long  = max(exchanges, key=lambda x: x["funding_rate_pct"])
        best_short = min(exchanges, key=lambda x: x["funding_rate_pct"])
        signals["funding_avg_pct"]         = avg_rate
        signals["funding_best_long_pct"]   = best_long["funding_rate_pct"]
        signals["funding_best_long_ex"]    = best_long["exchange"]
        signals["funding_best_short_pct"]  = best_short["funding_rate_pct"]
        signals["funding_best_short_ex"]   = best_short["exchange"]
        signals["funding_annualized_pct"]  = avg_rate * 3 * 365
        signals["funding_data_ok"]         = True
    else:
        # Exchanges list came back empty — API may have failed silently.
        # L/S ratio and OI are still valid; we flag the gap and lean on them.
        signals["funding_avg_pct"]         = 0.0
        signals["funding_best_long_pct"]   = 0.0
        signals["funding_best_long_ex"]    = "unknown"
        signals["funding_best_short_pct"]  = 0.0
        signals["funding_best_short_ex"]   = "unknown"
        signals["funding_annualized_pct"]  = 0.0
        signals["funding_data_ok"]         = False

    # 2. Open interest — conviction check
    # Rising OI = the dominant side (whichever direction funding favors) is growing
    # L/S ratio > 1.0 = more longs = supports positive funding
    # L/S ratio < 1.0 = more shorts = supports negative funding
    oi_raw = session.call("open_interest", {"symbol": ASSET})
    oi     = oi_raw.get("result", oi_raw)
    signals["oi_change_24h_pct"] = oi.get("oi_change_24h_pct", 0)
    signals["oi_change_1h_pct"]  = oi.get("oi_change_1h_pct",  0)
    signals["long_short_ratio"]  = oi.get("long_short_ratio",  1.0)

    # 3. Fear & Greed — crowding check (symmetric)
    # Extreme greed (>79): long carry crowded, sentiment flip risk
    # Extreme fear  (<21): short carry crowded, sentiment flip risk
    fg_raw = session.call("fear_greed_index", {})
    fg     = fg_raw.get("result", fg_raw)
    signals["fear_greed_value"] = fg.get("value", 50)
    signals["fear_greed_label"] = fg.get("value_classification", "Neutral")

    # 4. Whale activity — tail risk (same check regardless of direction)
    whale_raw = session.call("whale_activity", {"token": ASSET, "min_usd": 500000})
    whales    = whale_raw.get("result", whale_raw)
    signals["whale_volume_usd"]     = whales.get("total_volume_usd", 0)
    signals["whale_transfer_count"] = len(whales.get("large_transfers", []))

    # Derived: directional lean from non-funding signals
    # Used to give context when funding data is missing or rate is near zero.
    # L/S ratio is the strongest indicator — it reflects actual positioning.
    # OI momentum amplifies it. F/G gives sentiment confirmation.
    signals["lean"] = _compute_lean(signals)

    signals["cost"] = session.spent()
    return signals


def _compute_lean(signals: dict) -> str:
    """
    Directional lean from non-funding signals only.
    Returns "long", "short", or "neutral".

    This is NOT an entry signal — it's context for when funding data is
    missing or rate is near zero. Tells us which direction is building.
    """
    ls       = signals.get("long_short_ratio",   1.0)
    oi_24h   = signals.get("oi_change_24h_pct",  0)
    oi_1h    = signals.get("oi_change_1h_pct",   0)
    fg       = signals.get("fear_greed_value",    50)

    long_score  = 0
    short_score = 0

    # L/S ratio — strongest signal, double weight
    if   ls > 1.3: long_score  += 2
    elif ls > 1.0: long_score  += 1
    elif ls < 0.7: short_score += 2
    elif ls < 1.0: short_score += 1

    # OI momentum — is the dominant side growing?
    if   oi_24h >  5 or oi_1h >  3: long_score  += 1
    elif oi_24h < -5 or oi_1h < -3: short_score += 1

    # Fear & Greed — sentiment confirmation
    # Note: extreme fear + longs dominating (L/S > 1) is a divergence signal —
    # conviction longs holding despite fear is bullish. Don't double-penalise.
    if fg > 60:
        long_score += 1
    elif fg < 40 and ls < 1.0:
        # Shorts dominating AND fear — consistent short signal
        short_score += 1
    # If fg < 40 but L/S > 1.0, longs are holding despite fear — no short score added

    if long_score  > short_score: return "long"
    if short_score > long_score:  return "short"
    return "neutral"


# ── Decision logic ─────────────────────────────────────────────────────────────

def _decide(signals: dict, context: dict) -> tuple:
    """
    Returns (action, direction).
    action:    "enter_long_carry" | "enter_short_carry" |
               "exit_long_carry"  | "exit_short_carry"  |
               "hold" | "skip"
    direction: "long" | "short" | None
    """
    in_position = context.get("carry_position",  False)
    direction   = context.get("carry_direction", None)   # "long" | "short"

    funding   = signals.get("funding_avg_pct",    0)
    oi_change = signals.get("oi_change_24h_pct",  0)
    ls_ratio  = signals.get("long_short_ratio",   1.0)
    fg        = signals.get("fear_greed_value",   50)
    whale_vol = signals.get("whale_volume_usd",   0)

    # ── Exit logic: check open position ───────────────────────────────────────
    if in_position and direction == "long":
        # Exit long carry if rate dropped too low or whale risk appeared
        if funding < FUNDING_EXIT_THRESHOLD:
            return "exit_long_carry", None
        if whale_vol > MAX_WHALE_VOL_USD:
            return "exit_long_carry", None
        return "hold", "long"

    if in_position and direction == "short":
        # Exit short carry if rate is no longer negative enough or whale risk
        if funding > -FUNDING_EXIT_THRESHOLD:
            return "exit_short_carry", None
        if whale_vol > MAX_WHALE_VOL_USD:
            return "exit_short_carry", None
        return "hold", "short"

    # ── Entry logic: no open position ─────────────────────────────────────────

    # Hard blocks that prevent entry in either direction
    if abs(funding) < FUNDING_ENTER_THRESHOLD:
        return "skip", None   # Rate too low regardless of direction
    if oi_change < OI_MIN_CHANGE:
        return "skip", None   # Dominant side actively unwinding
    if whale_vol > MAX_WHALE_VOL_USD:
        return "skip", None   # Whale activity — wait

    # Long carry: funding positive, longs dominating, greed zone
    if funding > FUNDING_ENTER_THRESHOLD:
        if ls_ratio < 1.0:
            return "skip", None                          # More shorts than longs despite positive funding — fragile
        if not (FG_LONG_MIN <= fg <= FG_LONG_MAX):
            return "skip", None                          # Outside the greed sweet spot
        return "enter_long_carry", "long"

    # Short carry: funding negative, shorts dominating, fear zone
    if funding < -FUNDING_ENTER_THRESHOLD:
        if ls_ratio > 1.0:
            return "skip", None                          # More longs than shorts despite negative funding — fragile
        if not (FG_SHORT_MIN <= fg <= FG_SHORT_MAX):
            return "skip", None                          # Outside the fear sweet spot (incl. extreme fear = crowded)
        return "enter_short_carry", "short"

    return "skip", None


# ── Human-readable explanation ─────────────────────────────────────────────────

def _explain(signals: dict, action: str, direction) -> str:
    funding      = signals.get("funding_avg_pct",       0)
    annual       = signals.get("funding_annualized_pct", 0)
    bl_pct       = signals.get("funding_best_long_pct",  0)
    bl_ex        = signals.get("funding_best_long_ex",   "?")
    bs_pct       = signals.get("funding_best_short_pct", 0)
    bs_ex        = signals.get("funding_best_short_ex",  "?")
    oi_24h       = signals.get("oi_change_24h_pct",      0)
    oi_1h        = signals.get("oi_change_1h_pct",       0)
    ls           = signals.get("long_short_ratio",        1.0)
    fg           = signals.get("fear_greed_value",        50)
    fg_label     = signals.get("fear_greed_label",        "")
    whale_vol    = signals.get("whale_volume_usd",        0)
    lean         = signals.get("lean",                   "neutral")
    data_ok      = signals.get("funding_data_ok",        True)

    # Base context — always shown
    base = (
        f"Funding: {funding:+.4f}%/8h {'(data missing — API returned empty)' if not data_ok else ''}. "
        f"OI 24h: {oi_24h:+.1f}% / 1h: {oi_1h:+.1f}%, L/S: {ls:.2f}. "
        f"Fear/Greed: {fg} ({fg_label}). Whale vol: ${whale_vol:,.0f}."
    )

    # Action-specific suffix
    if action == "enter_long_carry":
        return base + f" → ENTER LONG CARRY — best rate {bl_pct:+.4f}% on {bl_ex} (~{annual:.1f}% APY)."

    if action == "enter_short_carry":
        return base + f" → ENTER SHORT CARRY — best rate {bs_pct:+.4f}% on {bs_ex} (~{abs(annual):.1f}% APY)."

    if action in ("exit_long_carry", "exit_short_carry"):
        reason = "funding dropped below exit threshold" if abs(funding) < FUNDING_EXIT_THRESHOLD else "whale activity detected"
        return base + f" → {action.upper().replace('_', ' ')} — {reason}."

    if action == "hold":
        carry_type = "long" if direction == "long" else "short"
        return base + f" → HOLD {carry_type.upper()} CARRY — conditions still valid."

    # SKIP — most informative case: explain lean and what would trigger entry
    if lean == "long":
        needed = max(0, FUNDING_ENTER_THRESHOLD - funding)
        watch  = f"Leaning LONG (L/S {ls:.2f}, OI {oi_24h:+.1f}%/24h). "
        watch += f"Need funding to rise +{needed:.4f}%/8h to trigger long carry entry."
        if not data_ok:
            watch += " (Note: funding data missing this tick — L/S suggests positive rate is plausible.)"
    elif lean == "short":
        needed = max(0, FUNDING_ENTER_THRESHOLD + funding)
        watch  = f"Leaning SHORT (L/S {ls:.2f}, OI {oi_24h:+.1f}%/24h). "
        watch += f"Need funding to fall -{needed:.4f}%/8h to trigger short carry entry."
        if not data_ok:
            watch += " (Note: funding data missing this tick.)"
    else:
        watch = "No directional lean — signals are mixed or flat. Nothing to watch."

    return base + f" → SKIP. {watch}"


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    from dotenv import load_dotenv

    _env_path = os.path.join(_here, "../..", ".env")
    load_dotenv(_env_path)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # Simulate different position states via args
    # --long-position   = currently in long carry
    # --short-position  = currently in short carry
    # (default)         = no position
    if "--long-position" in sys.argv:
        context = {"carry_position": True, "carry_direction": "long", "position_size_usd": 10000}
        pos_label = "LONG CARRY OPEN"
    elif "--short-position" in sys.argv:
        context = {"carry_position": True, "carry_direction": "short", "position_size_usd": 10000}
        pos_label = "SHORT CARRY OPEN"
    else:
        context   = {"carry_position": False, "carry_direction": None, "position_size_usd": 0}
        pos_label = "NO POSITION"

    print(f"\n{'═'*60}")
    print(f"  AgentPay — Funding Carry Routine")
    print(f"  Asset: {ASSET} | Position: {pos_label}")
    print(f"{'═'*60}\n")

    result = run(context=context)

    print(f"  Action:    {result.get('action', '?').upper()}")
    print(f"  Direction: {result.get('direction') or '—'}")
    print(f"  Lean:      {result.get('lean', '—')}")
    print(f"  Reasoning: {result.get('reasoning', '?')}")
    print(f"  Cost:      {result.get('cost', '?')}")
    print()

    if "--verbose" in sys.argv:
        print("  Raw signals:")
        for k, v in result.get("signals", {}).items():
            if k != "cost":
                print(f"    {k:<30} {v}")
    print()
