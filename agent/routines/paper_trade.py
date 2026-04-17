"""
paper_trade.py — Simulated funding carry, no exchange connection needed.

Runs a loop that:
  1. Calls funding_carry.run() on each tick (real AgentPay signals, real cost)
  2. Calls token_price to get the current ETH price
  3. Simulates entry/exit/hold decisions — no actual trades placed
  4. Tracks virtual P&L: funding collected vs signal costs vs leg delta

This is the fastest way to validate the strategy before connecting to Binance.
Real market data, real signal cost ($0.008/tick), zero exchange risk.

Usage:
    # Run with default 10-minute ticks (enough to see state changes)
    STELLAR_SECRET_KEY=S... python3 agent/routines/paper_trade.py

    # Faster loop for quick testing (every 60 seconds)
    STELLAR_SECRET_KEY=S... python3 agent/routines/paper_trade.py --interval 60

    # Run for a fixed number of ticks then stop
    STELLAR_SECRET_KEY=S... python3 agent/routines/paper_trade.py --ticks 6

    # Show full signal detail on every tick
    STELLAR_SECRET_KEY=S... python3 agent/routines/paper_trade.py --verbose

Cost: $0.009 per tick ($0.008 carry signals + $0.001 token_price for P&L tracking)
"""

import os
import sys
import time
import argparse
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "../.."))

from dotenv import load_dotenv
load_dotenv(os.path.join(_here, "../..", ".env"))

from agent.wallet import AgentWallet, Session
from agent.routines.funding_carry import run as carry_signal

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

_DEFAULT_GATEWAYS = {
    "mainnet": "https://gateway-production-2cc2.up.railway.app",
    "testnet": "https://gateway-testnet-production.up.railway.app",
}
NETWORK = os.environ.get("AGENTPAY_NETWORK", "mainnet")
GATEWAY = os.environ.get("AGENTPAY_GATEWAY_URL", _DEFAULT_GATEWAYS[NETWORK])
ASSET   = os.environ.get("CARRY_ASSET", "ETH")


# ── Paper position tracker ─────────────────────────────────────────────────────

class PaperPosition:
    """Tracks a simulated carry position with virtual P&L."""

    def __init__(self, size_usd: float = 1000.0):
        self.size_usd      = size_usd      # USD value of each leg
        self.direction     = None          # "long" | "short" | None
        self.entry_price   = None          # ETH price when we entered
        self.entry_time    = None
        self.funding_collected = 0.0       # cumulative funding received (USD)
        self.signal_cost   = 0.0           # cumulative AgentPay spend (USD)
        self.trades        = []            # log of simulated trades

    @property
    def is_open(self) -> bool:
        return self.direction is not None

    def enter(self, direction: str, price: float, funding_rate: float):
        self.direction   = direction
        self.entry_price = price
        self.entry_time  = datetime.now()
        trade = {
            "type":      f"ENTER_{direction.upper()}_CARRY",
            "price":     price,
            "size_usd":  self.size_usd,
            "timestamp": self.entry_time.strftime("%H:%M:%S"),
        }
        self.trades.append(trade)
        return trade

    def collect_funding(self, funding_rate_pct: float, price: float):
        """
        Called when we're in a position and a tick passes.
        Estimates funding earned based on current rate.
        (In reality this would be the actual funding payment from the exchange.)
        """
        if not self.is_open:
            return 0.0

        # funding_rate_pct is per 8h. We pro-rate by time since last tick.
        # For paper trading we just apply it once per tick as an approximation.
        if self.direction == "long" and funding_rate_pct > 0:
            earned = self.size_usd * (funding_rate_pct / 100)
        elif self.direction == "short" and funding_rate_pct < 0:
            earned = self.size_usd * (abs(funding_rate_pct) / 100)
        else:
            earned = 0.0

        self.funding_collected += earned
        return earned

    def exit(self, price: float) -> dict:
        """
        Close the position. Returns P&L summary.
        Carry is delta-neutral so price P&L should be near zero.
        """
        if not self.is_open:
            return {}

        duration_min = (datetime.now() - self.entry_time).seconds // 60

        # Delta P&L: should be ~0 (legs cancel). Shows any slippage/drift.
        if self.direction == "long":
            # Spot leg: +price change. Perp leg: -price change. Net ≈ 0.
            spot_pnl = self.size_usd * (price - self.entry_price) / self.entry_price
            perp_pnl = self.size_usd * (self.entry_price - price) / self.entry_price
            delta_pnl = spot_pnl + perp_pnl  # should be ~0
        else:
            spot_pnl  = self.size_usd * (self.entry_price - price) / self.entry_price
            perp_pnl  = self.size_usd * (price - self.entry_price) / self.entry_price
            delta_pnl = spot_pnl + perp_pnl  # should be ~0

        total_pnl = self.funding_collected + delta_pnl - self.signal_cost

        trade = {
            "type":      f"EXIT_{self.direction.upper()}_CARRY",
            "price":     price,
            "size_usd":  self.size_usd,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "duration_min": duration_min,
            "funding_collected_usd": round(self.funding_collected, 6),
            "delta_pnl_usd":         round(delta_pnl, 4),
            "signal_cost_usd":       round(self.signal_cost, 4),
            "total_pnl_usd":         round(total_pnl, 6),
        }
        self.trades.append(trade)

        # Reset
        self.direction         = None
        self.entry_price       = None
        self.entry_time        = None
        self.funding_collected = 0.0

        return trade

    def unrealized_pnl(self, current_price: float) -> float:
        """Current P&L if we closed right now. Should be ~0 for carry."""
        if not self.is_open or not self.entry_price:
            return 0.0
        if self.direction == "long":
            return (self.size_usd * (current_price - self.entry_price) / self.entry_price
                    + self.size_usd * (self.entry_price - current_price) / self.entry_price
                    + self.funding_collected)
        else:
            return (self.size_usd * (self.entry_price - current_price) / self.entry_price
                    + self.size_usd * (current_price - self.entry_price) / self.entry_price
                    + self.funding_collected)


# ── Price fetcher ──────────────────────────────────────────────────────────────

def get_eth_price(secret: str) -> float:
    """Fetch current ETH price via AgentPay token_price ($0.001)."""
    try:
        wallet = AgentWallet(secret_key=secret, network=NETWORK)
        with Session(wallet, gateway_url=GATEWAY, max_spend="0.005") as session:
            r = session.call("token_price", {"symbol": ASSET})
            data = r.get("result", r)
            return float(data.get("price_usd", 0))
    except Exception as e:
        logging.warning(f"Price fetch failed: {e}")
        return 0.0


# ── Main simulation loop ───────────────────────────────────────────────────────

def run_paper(interval_seconds: int = 600, max_ticks: int = None, verbose: bool = False):
    # On testnet, also check TEST_AGENT_SECRET_KEY (mirrors week2_test.py pattern)
    secret = (
        os.environ.get("STELLAR_SECRET_KEY") or
        (os.environ.get("TEST_AGENT_SECRET_KEY") if NETWORK == "testnet" else None) or
        ""
    )
    if not secret:
        key_hint = "STELLAR_SECRET_KEY or TEST_AGENT_SECRET_KEY" if NETWORK == "testnet" else "STELLAR_SECRET_KEY"
        print(f"ERROR: Set {key_hint} in your .env or pass inline.")
        sys.exit(1)

    size_usd = float(os.environ.get("PAPER_POSITION_SIZE", "1000"))
    position = PaperPosition(size_usd=size_usd)

    DIVIDER = "═" * 62

    network_label = "TESTNET (free signals)" if NETWORK == "testnet" else "MAINNET"
    print(f"\n{DIVIDER}")
    print(f"  AgentPay — Funding Carry Paper Trade")
    print(f"  Network:  {network_label}")
    print(f"  Asset:    {ASSET} | Size: ${size_usd:,.0f}/leg | Interval: {interval_seconds}s")
    print(f"  Ticks:    {max_ticks or '∞'}  |  Window: ~{((max_ticks or 0) * interval_seconds / 3600):.1f}h")
    print(f"  Cost:     ~${(max_ticks or 0) * 0.009:.3f} USDC total ({NETWORK})")
    print(f"  Press Ctrl+C to stop early")
    print(f"{DIVIDER}\n")

    tick      = 0
    total_cost = 0.0

    while True:
        tick += 1
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        print(f"  ── Tick {tick}  {now} {'─'*30}")

        # 1. Get current price (for P&L tracking)
        eth_price = get_eth_price(secret)
        price_str = f"${eth_price:,.2f}" if eth_price > 0 else "unavailable"

        # 2. Run carry signal
        context = {
            "carry_position":    position.is_open,
            "carry_direction":   position.direction,
            "position_size_usd": size_usd,
        }
        signal = carry_signal(context)
        action = signal.get("action", "skip")
        lean   = signal.get("lean", "neutral")
        cost   = signal.get("cost", "$0.000")

        # Track signal spend
        try:
            tick_cost = float(cost.replace("$", ""))
            total_cost += tick_cost
        except Exception:
            pass

        # 3. Parse funding rate from signals (0.0 if missing)
        funding_rate = signal.get("signals", {}).get("funding_avg_pct", 0.0)

        # 4. Simulate the action
        trade_note = ""
        if action == "enter_long_carry" and not position.is_open:
            trade = position.enter("long", eth_price, funding_rate)
            trade_note = f"  ▶ SIMULATED: BUY {size_usd/eth_price:.4f} ETH spot @ {price_str} + SELL ETH-PERP"

        elif action == "enter_short_carry" and not position.is_open:
            trade = position.enter("short", eth_price, funding_rate)
            trade_note = f"  ▶ SIMULATED: SELL {size_usd/eth_price:.4f} ETH spot @ {price_str} + BUY ETH-PERP"

        elif action in ("exit_long_carry", "exit_short_carry") and position.is_open:
            trade = position.exit(eth_price)
            trade_note = (
                f"  ▶ SIMULATED EXIT @ {price_str}\n"
                f"    Funding collected: ${trade.get('funding_collected_usd', 0):.6f}\n"
                f"    Delta P&L:         ${trade.get('delta_pnl_usd', 0):.4f}  (should be ~$0)\n"
                f"    Signal costs:      ${trade.get('signal_cost_usd', 0):.4f}\n"
                f"    Net P&L:           ${trade.get('total_pnl_usd', 0):.6f}"
            )

        elif position.is_open:
            # Collect simulated funding while holding
            earned = position.collect_funding(funding_rate, eth_price)
            if earned > 0:
                trade_note = f"  ✦ Funding tick: +${earned:.6f}"
            position.signal_cost += tick_cost

        # 5. Print tick summary
        status = "OPEN" if position.is_open else "NONE"
        direction_str = f"({position.direction.upper()} CARRY)" if position.is_open else ""

        print(f"  ETH price:  {price_str}")
        print(f"  Signal:     {action.upper()} | lean: {lean}")
        print(f"  Position:   {status} {direction_str}")

        if position.is_open and eth_price > 0:
            upnl = position.unrealized_pnl(eth_price)
            fund = position.funding_collected
            print(f"  Unrealized: ${upnl:.4f}  (funding: ${fund:.6f})")

        if trade_note:
            print(trade_note)

        if verbose:
            reasoning = signal.get("reasoning", "")
            print(f"\n  Reasoning: {reasoning}")

        data_ok = signal.get("signals", {}).get("funding_data_ok", True)
        if not data_ok:
            print(f"  ⚠  Funding data missing this tick (API returned empty)")

        print(f"  Tick cost:  {cost}  |  Total spent: ${total_cost:.4f}")
        print()

        # Stop condition
        if max_ticks and tick >= max_ticks:
            break

        # Wait for next tick
        try:
            print(f"  Next tick in {interval_seconds}s — Ctrl+C to stop\n")
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            break

    # ── Final summary ──────────────────────────────────────────────────────────
    duration_h = (tick * interval_seconds) / 3600

    # Aggregate P&L across all closed trades
    total_funding  = sum(t.get("funding_collected_usd", 0) for t in position.trades if "total_pnl_usd" in t)
    total_delta    = sum(t.get("delta_pnl_usd",         0) for t in position.trades if "total_pnl_usd" in t)
    total_net_pnl  = total_funding + total_delta - total_cost
    entries        = [t for t in position.trades if t["type"].startswith("ENTER")]
    exits          = [t for t in position.trades if t["type"].startswith("EXIT")]

    # If still open at end of window, include unrealised funding
    if position.is_open:
        total_funding += position.funding_collected
        total_net_pnl  = total_funding + total_delta - total_cost

    print(f"\n{DIVIDER}")
    print(f"  Paper Trade — Final Report")
    print(f"  {tick} ticks  |  {duration_h:.1f}h window  |  ${size_usd:,.0f} per leg")
    print(f"{'─'*62}")

    print(f"\n  Your ${size_usd:,.0f} paper investment — what happened:")
    print()

    if not entries:
        print(f"  Strategy never entered a position.")
        print(f"  Reason: conditions weren't met (funding near zero, extreme fear).")
        print(f"  Your ${size_usd:,.0f} stayed on the sideline — no gain, no loss on the trade.")
        print(f"  Only cost: ${total_cost:.4f} in AgentPay signal checks.")
    else:
        n_entries = len(entries)
        n_exits   = len(exits)
        print(f"  Trades entered:     {n_entries}")
        print(f"  Trades exited:      {n_exits}")
        if position.is_open:
            print(f"  Position at close:  STILL OPEN ({position.direction.upper()} CARRY)")
        print()
        print(f"  ┌─────────────────────────────────────────────────┐")
        print(f"  │  Funding collected   ${total_funding:>+10.6f}              │")
        print(f"  │  Delta P&L           ${total_delta:>+10.4f}  (should be ~$0) │")
        print(f"  │  Signal costs        ${-total_cost:>+10.4f}              │")
        print(f"  │  ─────────────────────────────────────────────  │")
        pnl_marker = "▲" if total_net_pnl >= 0 else "▼"
        print(f"  │  Net P&L             ${total_net_pnl:>+10.6f}  {pnl_marker}             │")
        print(f"  │  Return on ${size_usd:,.0f}    {(total_net_pnl/size_usd)*100:>+9.4f}%              │")
        if duration_h > 0:
            annualized = (total_net_pnl / size_usd) * (8760 / duration_h) * 100
            print(f"  │  Annualised (est.)   {annualized:>+9.2f}%              │")
        print(f"  └─────────────────────────────────────────────────┘")

    # Break-even analysis — always useful
    print(f"\n  Break-even analysis at current signal cost (${total_cost:.4f}/day):")
    daily_cost     = total_cost / max(duration_h / 24, 0.001)
    breakeven_rate = (daily_cost / (size_usd * 3)) * 100  # 3 payments/day
    breakeven_size = daily_cost / (0.01 / 100 * 3)         # at FUNDING_ENTER_THRESHOLD
    print(f"  • You need funding ≥ {breakeven_rate:.4f}%/8h to cover signal costs at ${size_usd:,.0f}")
    print(f"  • OR a position size ≥ ${breakeven_size:,.0f} to break even at 0.01%/8h threshold")
    print(f"  • At typical bull-market funding (0.05%/8h), ${size_usd:,.0f} earns")
    print(f"    ${size_usd * 0.0005 * 3:.2f}/day = ${size_usd * 0.0005 * 3 * 365:.0f}/year (~{0.05*3*365:.0f}% APY)")

    # Trade log
    if position.trades:
        print(f"\n  Trade log:")
        for t in position.trades:
            print(f"    {t['timestamp']}  {t['type']:<30}  @ ${t.get('price', 0):,.2f}")
            if "total_pnl_usd" in t:
                print(f"                   net: ${t['total_pnl_usd']:+.6f}  "
                      f"(funding ${t['funding_collected_usd']:+.6f} · "
                      f"delta ${t['delta_pnl_usd']:+.4f} · "
                      f"held {t['duration_min']}min)")

    print(f"\n  Total signal spend: ${total_cost:.4f} USDC (paid on Stellar)")
    print(f"{DIVIDER}\n")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgentPay funding carry paper trader")
    parser.add_argument("--interval", type=int,   default=600,   help="Seconds between ticks (default: 600)")
    parser.add_argument("--ticks",    type=int,   default=None,  help="Stop after N ticks (default: run forever)")
    parser.add_argument("--size",     type=float, default=None,  help="Position size in USD per leg (default: $1000)")
    parser.add_argument("--testnet",  action="store_true",        help="Use testnet gateway (free faucet USDC)")
    parser.add_argument("--verbose",  action="store_true",        help="Print full signal reasoning each tick")
    args = parser.parse_args()

    if args.size:
        os.environ["PAPER_POSITION_SIZE"] = str(args.size)

    if args.testnet:
        os.environ["AGENTPAY_NETWORK"]    = "testnet"
        os.environ["AGENTPAY_GATEWAY_URL"] = _DEFAULT_GATEWAYS["testnet"]
        # Re-read so the module-level NETWORK/GATEWAY vars update
        import importlib
        import agent.routines.funding_carry as _fc
        _fc.NETWORK = "testnet"
        _fc.GATEWAY = _DEFAULT_GATEWAYS["testnet"]
        globals()["NETWORK"] = "testnet"
        globals()["GATEWAY"] = _DEFAULT_GATEWAYS["testnet"]

    run_paper(
        interval_seconds=args.interval,
        max_ticks=args.ticks,
        verbose=args.verbose,
    )
