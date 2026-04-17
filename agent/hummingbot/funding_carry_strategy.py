"""
funding_carry_strategy.py — Hummingbot script for funding rate carry.

SETUP
-----
1. Copy this file to: hummingbot/scripts/funding_carry_strategy.py
2. Copy funding_carry_config.yml to: hummingbot/conf/scripts/funding_carry_config.yml
3. Copy the agentpay/ folder to somewhere Hummingbot can reach, e.g. ~/agentpay
4. Update AGENTPAY_PATH below to point at it
5. In Hummingbot:
     connect binance
     connect binance_perpetual
     start --script funding_carry_strategy.py --conf conf/scripts/funding_carry_config.yml

WHAT THIS DOES
--------------
On every signal interval (default every 4h), calls funding_carry.run() which pays
~$0.008 USDC via AgentPay to gather 4 signals:
  funding_rates + open_interest + fear_greed_index + whale_activity

Based on the result it opens, holds, or closes a delta-neutral position:

  LONG CARRY:  BUY ETH spot (binance) + SELL ETH-PERP (binance_perpetual)
               → collect positive funding from longs paying shorts

  SHORT CARRY: SELL ETH spot (binance) + BUY ETH-PERP (binance_perpetual)
               → collect negative funding from shorts paying longs

  EXIT:        Close both legs at market simultaneously
  HOLD / SKIP: Do nothing

CONDOR INTEGRATION
------------------
This script works standalone in Hummingbot. To add Condor's LLM layer on top:
  - The signal dict (action + reasoning) passes directly to Condor's OODA loop
  - Condor reads the reasoning string and can override the action before execution
  - No code changes needed here — Condor wraps the on_tick() flow externally

Press 's' in Hummingbot to view live status (position, last signal, next check).
"""

import os
import sys
import time
import logging
from decimal import Decimal
from typing import Optional

# ── Point at your agentpay installation ───────────────────────────────────────
AGENTPAY_PATH = os.path.expanduser("~/agentpay")
sys.path.insert(0, AGENTPAY_PATH)

from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.core.data_type.common import OrderType, PositionAction
from pydantic import Field

from agent.routines.funding_carry import run as carry_signal

logger = logging.getLogger(__name__)


class FundingCarryConfig:
    """Config fields — loaded from funding_carry_config.yml by Hummingbot."""
    spot_connector:           str     = "binance"
    perp_connector:           str     = "binance_perpetual"
    trading_pair:             str     = "ETH-USDT"
    position_size_usd:        Decimal = Decimal("1000")
    signal_interval_seconds:  int     = 14400
    stellar_secret_key:       str     = ""


class FundingCarryStrategy(ScriptStrategyBase):
    """
    Funding rate carry strategy — reads AgentPay signals, executes two-legged trades.
    """

    # Hummingbot reads these to know which connectors to load
    markets = {
        FundingCarryConfig.spot_connector: {FundingCarryConfig.trading_pair},
        FundingCarryConfig.perp_connector: {FundingCarryConfig.trading_pair},
    }

    def __init__(self, connectors: dict, config: FundingCarryConfig = None):
        super().__init__(connectors)
        self.config = config or FundingCarryConfig()

        # Set STELLAR_SECRET_KEY in env so carry_signal() can find it
        if self.config.stellar_secret_key:
            os.environ["STELLAR_SECRET_KEY"] = self.config.stellar_secret_key

        # Internal state
        self._in_position:   bool           = False
        self._direction:     Optional[str]  = None    # "long" | "short"
        self._entry_amount:  Optional[Decimal] = None  # exact amount placed at entry
        self._last_signal:   Optional[dict] = None
        self._last_check_ts: float          = 0

        self.logger().info("[carry] Strategy initialised")

    # ── Tick ──────────────────────────────────────────────────────────────────

    def on_tick(self):
        """Called ~every second by Hummingbot. Throttled to signal_interval."""
        now = time.time()
        elapsed = now - self._last_check_ts

        if elapsed < self.config.signal_interval_seconds:
            return

        self._last_check_ts = now
        self._check_and_act()

    # ── Signal → action ───────────────────────────────────────────────────────

    def _check_and_act(self):
        context = {
            "carry_position":    self._in_position,
            "carry_direction":   self._direction,
            "position_size_usd": float(self.config.position_size_usd),
        }

        try:
            signal = carry_signal(context)
        except Exception as e:
            self.logger().error(f"[carry] Signal call failed: {e}")
            return

        self._last_signal = signal
        action = signal.get("action", "skip")

        self.logger().info(
            f"[carry] {action.upper()} | lean={signal.get('lean')} | "
            f"cost={signal.get('cost')} | {signal.get('reasoning', '')[:140]}"
        )

        dispatch = {
            "enter_long_carry":  lambda: self._enter("long"),
            "enter_short_carry": lambda: self._enter("short"),
            "exit_long_carry":   lambda: self._exit("long"),
            "exit_short_carry":  lambda: self._exit("short"),
        }
        if action in dispatch:
            dispatch[action]()
        elif action == "error":
            self.logger().error(f"[carry] Signal error: {signal.get('reasoning')}")
        # hold / skip → do nothing

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _enter(self, direction: str):
        if self._in_position:
            self.logger().warning("[carry] Already in position — skipping entry")
            return

        amount = self._calc_amount()
        if not amount:
            return

        try:
            if direction == "long":
                # Leg 1: buy spot (own the ETH)
                self.buy(
                    connector_name=self.config.spot_connector,
                    trading_pair=self.config.trading_pair,
                    amount=amount,
                    order_type=OrderType.MARKET,
                )
                # Leg 2: sell perp (short the futures, collect positive funding)
                self.sell(
                    connector_name=self.config.perp_connector,
                    trading_pair=self.config.trading_pair,
                    amount=amount,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.OPEN,
                )
                self.logger().info(
                    f"[carry] ENTERED LONG CARRY — {amount} ETH | "
                    f"spot BUY + perp SHORT"
                )

            elif direction == "short":
                # Leg 1: sell spot (short the ETH)
                self.sell(
                    connector_name=self.config.spot_connector,
                    trading_pair=self.config.trading_pair,
                    amount=amount,
                    order_type=OrderType.MARKET,
                )
                # Leg 2: buy perp (long the futures, collect negative funding)
                self.buy(
                    connector_name=self.config.perp_connector,
                    trading_pair=self.config.trading_pair,
                    amount=amount,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.OPEN,
                )
                self.logger().info(
                    f"[carry] ENTERED SHORT CARRY — {amount} ETH | "
                    f"spot SELL + perp LONG"
                )

            self._in_position  = True
            self._direction    = direction
            self._entry_amount = amount

        except Exception as e:
            self.logger().error(f"[carry] Entry failed ({direction}): {e}")

    # ── Exit ──────────────────────────────────────────────────────────────────

    def _exit(self, direction: str):
        if not self._in_position:
            self.logger().warning("[carry] No open position to exit")
            return

        # Use recorded entry amount if available; fall back to current calc
        amount = self._entry_amount or self._calc_amount()
        if not amount:
            return

        try:
            if direction == "long":
                # Close leg 1: sell spot
                self.sell(
                    connector_name=self.config.spot_connector,
                    trading_pair=self.config.trading_pair,
                    amount=amount,
                    order_type=OrderType.MARKET,
                )
                # Close leg 2: buy back perp
                self.buy(
                    connector_name=self.config.perp_connector,
                    trading_pair=self.config.trading_pair,
                    amount=amount,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.CLOSE,
                )
                self.logger().info(f"[carry] EXITED LONG CARRY — {amount} ETH")

            elif direction == "short":
                # Close leg 1: buy back spot
                self.buy(
                    connector_name=self.config.spot_connector,
                    trading_pair=self.config.trading_pair,
                    amount=amount,
                    order_type=OrderType.MARKET,
                )
                # Close leg 2: sell back perp
                self.sell(
                    connector_name=self.config.perp_connector,
                    trading_pair=self.config.trading_pair,
                    amount=amount,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.CLOSE,
                )
                self.logger().info(f"[carry] EXITED SHORT CARRY — {amount} ETH")

            self._in_position  = False
            self._direction    = None
            self._entry_amount = None

        except Exception as e:
            self.logger().error(f"[carry] Exit failed ({direction}): {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _calc_amount(self) -> Optional[Decimal]:
        """Convert position_size_usd to ETH amount at current mid price."""
        try:
            connector = self.connectors[self.config.spot_connector]
            price = connector.get_mid_price(self.config.trading_pair)
            if not price or price <= 0:
                raise ValueError("Invalid mid price")
            # Round to Binance's minimum ETH precision (0.0001)
            return (self.config.position_size_usd / price).quantize(Decimal("0.0001"))
        except Exception as e:
            self.logger().error(f"[carry] Could not calculate order amount: {e}")
            return None

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def format_status(self) -> str:
        """Press 's' in Hummingbot to see this."""
        next_check = max(
            0,
            self.config.signal_interval_seconds - (time.time() - self._last_check_ts)
        )
        lines = [
            "── AgentPay Funding Carry ──────────────────────────────",
            f"  Pair:       {self.config.trading_pair}",
            f"  Position:   {'OPEN — ' + (self._direction or '?').upper() + ' CARRY' if self._in_position else 'NONE'}",
            f"  Size:       ${self.config.position_size_usd} USD per leg",
            f"  Next check: {next_check:.0f}s",
            "",
        ]
        if self._last_signal:
            s = self._last_signal
            lines += [
                "  Last signal ─────────────────────────────────────────",
                f"    Action:    {s.get('action', '?').upper()}",
                f"    Direction: {s.get('direction') or '—'}",
                f"    Lean:      {s.get('lean', '—')}",
                f"    Cost:      {s.get('cost', '?')}",
                f"    {s.get('reasoning', '')[:120]}",
            ]
        return "\n".join(lines)
