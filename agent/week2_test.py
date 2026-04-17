"""
week2_test.py — Smoke test for Week 2 tools: open_interest + orderbook_depth.

Tests both new tools with real USDC payments on Stellar testnet (free faucet).
Total cost: $0.004 testnet USDC (2 × $0.002)

Usage (from project root):
    python agent/week2_test.py

Required in .env:
    STELLAR_SECRET_KEY=S...   (testnet wallet secret)

Override gateway or network via env:
    WEEK2_GATEWAY_URL=https://... python agent/week2_test.py
"""

import os
import sys
import json
import logging
from decimal import Decimal

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(_here, "..", ".env"))

from agent.wallet import AgentWallet, Session, BudgetExceeded

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

# Network: default testnet; override with WEEK2_NETWORK=mainnet
NETWORK = os.environ.get("WEEK2_NETWORK", "testnet")

_GATEWAY_DEFAULTS = {
    "mainnet": "https://gateway-production-2cc2.up.railway.app",
    "testnet": "https://gateway-testnet-production.up.railway.app",
}
GATEWAY_URL  = os.environ.get("WEEK2_GATEWAY_URL", _GATEWAY_DEFAULTS[NETWORK])
AGENT_SECRET = os.getenv("STELLAR_SECRET_KEY") or os.getenv("TEST_AGENT_SECRET_KEY", "")
MAX_BUDGET   = "0.02"

DIVIDER = "═" * 58

def hr(title=""):
    print(f"\n  {'─'*54}")
    if title:
        print(f"  {title}")
        print(f"  {'─'*54}")

def main():
    if not AGENT_SECRET:
        print("ERROR: Set STELLAR_SECRET_KEY in .env")
        sys.exit(1)

    wallet = AgentWallet(
        secret_key=AGENT_SECRET,
        network=NETWORK,
    )
    balance = wallet.get_usdc_balance()

    print(f"\n{DIVIDER}")
    print(f"  AgentPay — Week 2 Tool Test")
    print(f"  open_interest + orderbook_depth")
    print(f"{DIVIDER}")
    print(f"  Agent:   {wallet.public_key}")
    print(f"  Network: {wallet.network}")
    print(f"  Balance: {balance} USDC")
    print(f"  Budget:  ${MAX_BUDGET} USDC")
    print(f"{DIVIDER}")

    results = {}

    with Session(wallet=wallet, gateway_url=GATEWAY_URL, max_spend=MAX_BUDGET) as session:

        # ── Test 1: open_interest (ETH) ───────────────────────────────────────
        hr("Test 1 — open_interest (ETH)")
        print()
        try:
            r = session.call("open_interest", {"symbol": "ETH"})
            d = r.get("result", r)
            results["open_interest"] = d

            print(f"    Asset:            {d.get('asset', '?')}")
            print(f"    Price:            ${d.get('price_usd', 0):>12,.2f}")
            print(f"    Total OI (USD):   ${d.get('total_oi_usd', 0):>15,.0f}")
            def _pct(v): return f"{v:>+.4f}%" if v is not None else "N/A"
            print(f"    OI change 1h:     {_pct(d.get('oi_change_1h_pct'))}")
            print(f"    OI change 24h:    {_pct(d.get('oi_change_24h_pct'))}")
            print(f"    Long/Short ratio: {d.get('long_short_ratio', 'N/A')}")
            print()
            exchanges = d.get("exchanges", [])
            if exchanges:
                print(f"    {'Exchange':<12}  {'OI (USD)':>16}   {'1h%':>6}   {'24h%':>6}")
                print(f"    {'─'*12}  {'─'*16}   {'─'*6}   {'─'*6}")
                for ex in exchanges:
                    oi = ex.get("oi_usd") or (ex.get("oi_contracts", 0) * d.get("price_usd", 0))
                    ch1  = ex.get("oi_change_1h_pct")
                    ch24 = ex.get("oi_change_24h_pct")
                    print(f"    {ex.get('exchange','?'):<12}  ${oi:>15,.0f}   "
                          f"{f'{ch1:>+.2f}%' if ch1 is not None else 'N/A':>7}   "
                          f"{f'{ch24:>+.2f}%' if ch24 is not None else 'N/A':>7}")

            print(f"\n    ✅ open_interest passed — spent ${session._call_log[-1]['amount_usdc']}")
            print(f"    Remaining budget: {session.remaining()}")

        except BudgetExceeded as e:
            print(f"    ❌ SKIPPED — {e}")
        except Exception as e:
            print(f"    ❌ ERROR — {e}")
            results["open_interest_error"] = str(e)

        # ── Test 2: orderbook_depth (ETHUSDT on Binance) ──────────────────────
        hr("Test 2 — orderbook_depth (ETHUSDT, Binance)")
        print()
        try:
            r = session.call("orderbook_depth", {"symbol": "ETHUSDT", "exchange": "binance"})
            d = r.get("result", r)
            results["orderbook_depth"] = d

            print(f"    Pair:      {d.get('pair', '?')} on {d.get('exchange', '?')}")
            print(f"    Best bid:  ${d.get('best_bid', 0):>12,.2f}")
            print(f"    Best ask:  ${d.get('best_ask', 0):>12,.2f}")
            print(f"    Spread:    {d.get('spread_pct', 0):.4f}%")
            print()
            depth = d.get("depth", [])
            if depth:
                print(f"    {'Notional':>12}   {'Slippage':>10}")
                print(f"    {'─'*12}   {'─'*10}")
                for level in depth:
                    notional = level.get("notional_usd", 0)
                    slip = level.get("slippage_pct", 0)
                    print(f"    ${notional:>11,.0f}   {slip:>9.4f}%")

            print(f"\n    ✅ orderbook_depth passed — spent ${session._call_log[-1]['amount_usdc']}")
            print(f"    Remaining budget: {session.remaining()}")

        except BudgetExceeded as e:
            print(f"    ❌ SKIPPED — {e}")
        except Exception as e:
            print(f"    ❌ ERROR — {e}")
            results["orderbook_depth_error"] = str(e)

        summary = session.summary()

    # ── Summary ───────────────────────────────────────────────────────────────
    hr("Summary")
    passed = sum(1 for k in results if not k.endswith("_error"))
    failed = sum(1 for k in results if k.endswith("_error"))
    print(f"\n    Tools passed:  {passed}/2")
    print(f"    Tools failed:  {failed}/2")
    print(f"    Total spent:   {summary['spent_fmt']}")
    print(f"    Remaining:     {summary['remaining_fmt']}")

    # Demo narrative
    if "open_interest" in results and "orderbook_depth" in results:
        oi  = results["open_interest"]
        ob  = results["orderbook_depth"]
        oi_24h = oi.get("oi_change_24h_pct", 0)
        slip_250k = next(
            (l["slippage_pct"] for l in ob.get("depth", []) if l.get("notional_usd", 0) >= 200_000),
            None
        )
        print(f"""
  ─────────────────────────────────────────────────────
  Demo narrative:

  ETH open interest is {oi_24h:+.1f}% over 24h.
  A $250k sell on Binance would slip {f'{slip_250k:.3f}%' if slip_250k else 'N/A'}.
  Total data cost: {summary['spent_fmt']} USDC.
  ─────────────────────────────────────────────────────
""")

    # Save results
    out = os.path.join(_here, "week2_test_result.json")
    with open(out, "w") as f:
        json.dump({"results": results, "session": summary}, f, indent=2, default=str)
    print(f"  Full results saved → {out}\n")


if __name__ == "__main__":
    main()
