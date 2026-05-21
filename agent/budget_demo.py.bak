"""
budget_demo.py — Comprehensive ETH analysis within a $0.10 USDC budget.

The agent:
  1. Queries the gateway for all available tools and their prices
  2. Estimates total cost for a full ETH analysis
  3. Decides which tools to call in priority order
  4. Calls each tool, printing live spend tracking after every payment
  5. Ends with a full session summary and cost breakdown

Usage (from project root):
    python agent/budget_demo.py

Required in .env (or environment):
    STELLAR_SECRET_KEY=S...        # mainnet test agent (preferred)
    TEST_AGENT_SECRET_KEY=S...     # fallback
    AGENTPAY_GATEWAY_URL=...       # defaults to production mainnet
"""

import os
import sys
import json
import httpx
import logging
from decimal import Decimal

# ── Path + env setup ──────────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, ".."))

# Capture STELLAR_NETWORK before load_dotenv can overwrite it with .env value
_env_network = os.environ.get("STELLAR_NETWORK")

from dotenv import load_dotenv
load_dotenv(os.path.join(_here, "..", ".env"))

from agent.wallet import AgentWallet, Session, BudgetExceeded, _fmt

logging.basicConfig(
    level=logging.WARNING,           # suppress httpx noise
    format="%(asctime)s [%(levelname)s] %(message)s",
)

GATEWAY_URL  = os.getenv("AGENTPAY_GATEWAY_URL", "https://gateway-production-2cc2.up.railway.app")
AGENT_SECRET = os.getenv("STELLAR_SECRET_KEY") or os.getenv("TEST_AGENT_SECRET_KEY", "")
MAX_BUDGET   = "0.10"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bar(spent: Decimal, budget: Decimal, width: int = 36) -> str:
    pct = min(spent / budget, Decimal("1")) if budget > 0 else Decimal("0")
    filled = int(round(pct * width))
    color_on = color_off = ""
    if pct > Decimal("0.85"):
        color_on, color_off = "\033[91m", "\033[0m"   # red
    elif pct > Decimal("0.60"):
        color_on, color_off = "\033[93m", "\033[0m"   # yellow
    else:
        color_on, color_off = "\033[92m", "\033[0m"   # green
    bar = f"{color_on}{'█' * filled}{'░' * (width - filled)}{color_off}"
    return f"[{bar}]  {pct*100:5.1f}%"


def _print_live(session: Session, tool_name: str, cost: str):
    bar      = _bar(session._spent, session.max_spend)
    print(f"\n  Budget {bar}")
    print(f"  Spent: {session.spent()}   Remaining: {session.remaining()}   [{tool_name}: {_fmt(cost)}]\n")


def _section(title: str):
    print(f"\n  {'─'*54}")
    print(f"  {title}")
    print(f"  {'─'*54}")


def fetch_all_tools(gateway_url: str) -> list[dict]:
    """Fetch all active tools from gateway, sorted by price ascending."""
    resp = httpx.get(f"{gateway_url}/tools", timeout=8.0)
    resp.raise_for_status()
    tools = resp.json().get("tools", [])
    return sorted(tools, key=lambda t: Decimal(t.get("price_usdc", "0")))


def plan_eth_analysis(tools: list[dict], budget: str) -> list[dict]:
    """
    Choose which tools to call for a comprehensive ETH analysis.

    Priority (highest analytical value first):
      1. token_price      — must have: current price + momentum
      2. dex_liquidity    — market depth and volume
      3. gas_tracker      — network conditions
      4. whale_activity   — large holder sentiment
      5. dune_query       — deepest onchain data (most expensive)

    Only include tools that fit within the remaining budget.
    """
    priority = ["token_price", "dex_liquidity", "gas_tracker", "whale_activity", "dune_query"]
    tool_map = {t["name"]: t for t in tools}

    planned = []
    running_cost = Decimal("0")
    budget_d = Decimal(budget)

    for name in priority:
        tool = tool_map.get(name)
        if not tool:
            continue
        price = Decimal(tool["price_usdc"])
        if running_cost + price <= budget_d:
            planned.append(tool)
            running_cost += price

    return planned


# ── Main Demo ─────────────────────────────────────────────────────────────────

def main():
    if not AGENT_SECRET:
        print("ERROR: Set STELLAR_SECRET_KEY (or TEST_AGENT_SECRET_KEY) in .env")
        sys.exit(1)

    wallet = AgentWallet(
        secret_key=AGENT_SECRET,
        network=_env_network or "mainnet",
    )

    balance = wallet.get_usdc_balance()

    print(f"\n{'═'*58}")
    print(f"  AgentPay Budget Demo — Comprehensive ETH Analysis")
    print(f"{'═'*58}")
    print(f"  Agent:   {wallet.public_key}")
    print(f"  Network: {wallet.network}")
    print(f"  Balance: {balance} USDC")
    print(f"  Budget:  ${MAX_BUDGET} USDC (hard cap)")
    print(f"{'═'*58}")

    if Decimal(str(balance)) < Decimal(MAX_BUDGET):
        print(f"\n  WARNING: Balance ({balance}) below demo budget ({MAX_BUDGET}). Continuing anyway.\n")

    # ── Step 1: Discover available tools ─────────────────────────────────────
    _section("Step 1 — Discover available tools")
    try:
        all_tools = fetch_all_tools(GATEWAY_URL)
    except Exception as e:
        print(f"  ERROR: Cannot reach gateway at {GATEWAY_URL}: {e}")
        sys.exit(1)

    print(f"\n  {'Tool':<24} {'Price':>8}   {'Category':<12}  Description")
    print(f"  {'─'*24} {'─'*8}   {'─'*12}  {'─'*28}")
    for t in all_tools:
        print(
            f"  {t['name']:<24} {_fmt(t['price_usdc']):>8}   "
            f"{t.get('category','?'):<12}  {t.get('description','')[:40]}"
        )

    # ── Step 2: Plan the analysis ─────────────────────────────────────────────
    _section("Step 2 — Plan ETH analysis within $0.10 budget")
    planned = plan_eth_analysis(all_tools, MAX_BUDGET)

    total_est = sum(Decimal(t["price_usdc"]) for t in planned)
    print(f"\n  Agent decision: call {len(planned)} tools (estimated total: {_fmt(total_est)})\n")
    for i, t in enumerate(planned, 1):
        print(f"    {i}. {t['name']:<24} {_fmt(t['price_usdc'])}   — {t.get('description','')[:45]}")
    print(f"\n  Estimated spend: {_fmt(total_est)}   |   Headroom: {_fmt(Decimal(MAX_BUDGET) - total_est)}")

    # ── Step 3: Execute with live budget tracking ─────────────────────────────
    _section(f"Step 3 — Execute (paying per tool on Stellar {wallet.network})")
    print()

    results = {}

    with Session(wallet=wallet, gateway_url=GATEWAY_URL, max_spend=MAX_BUDGET) as session:

        # --- token_price -------------------------------------------------
        print("  [1/5] token_price — current ETH price and momentum")
        try:
            r = session.call("token_price", {"symbol": "ETH"})
            d = r.get("result", r)
            results["token_price"] = d
            print(f"    Price USD:     ${d.get('price_usd', 0):>12,.2f}")
            print(f"    24h change:    {d.get('change_24h_pct', 0):>+.4f}%")
            print(f"    Market cap:    ${d.get('market_cap_usd', 0):>15,.0f}")
            _print_live(session, "token_price", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    SKIPPED — {e}\n")

        # --- dex_liquidity -----------------------------------------------
        print("  [2/5] dex_liquidity — ETH/USDC market depth and volume")
        try:
            r = session.call("dex_liquidity", {"token_a": "ETH", "token_b": "USDC"})
            d = r.get("result", r)
            results["dex_liquidity"] = d
            print(f"    24h volume:    ${d.get('volume_24h_usd', 0):>15,.0f}")
            print(f"    Market cap:    ${d.get('market_cap_usd', 0):>15,.0f}")
            print(f"    Price (check): ${d.get('price_usd', 0):>12,.2f}")
            print(f"    All-time high: ${d.get('ath_usd', 0):>12,.2f}")
            _print_live(session, "dex_liquidity", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    SKIPPED — {e}\n")

        # --- gas_tracker -------------------------------------------------
        print("  [3/5] gas_tracker — Ethereum network conditions")
        try:
            r = session.call("gas_tracker", {})
            d = r.get("result", r)
            results["gas_tracker"] = d
            print(f"    Base fee:      {d.get('base_fee_gwei')} gwei")
            print(f"    Standard:      {d.get('standard_gwei')} gwei")
            print(f"    Fast:          {d.get('fast_gwei')} gwei")
            _print_live(session, "gas_tracker", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    SKIPPED — {e}\n")

        # --- whale_activity ----------------------------------------------
        print("  [4/5] whale_activity — large ETH/WETH transfers (>$500k)")
        try:
            r = session.call("whale_activity", {"token": "ETH", "min_usd": 500_000})
            d = r.get("result", r)
            results["whale_activity"] = d
            transfers = d.get("large_transfers", [])
            print(f"    Transfers ≥$500k:  {len(transfers)}")
            print(f"    Total volume:      ${d.get('total_volume_usd', 0):>15,.0f}")
            for tx in transfers[:4]:
                val = tx.get("usd_value") or 0
                print(
                    f"      {tx.get('from','?'):>12} → {tx.get('to','?'):<12}  "
                    f"${val:>12,.0f}  ({tx.get('minutes_ago','?')} min ago)"
                )
            _print_live(session, "whale_activity", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    SKIPPED — {e}\n")

        # --- dune_query --------------------------------------------------
        print("  [5/5] dune_query — live onchain ETH data (query 3810512)")
        try:
            r = session.call("dune_query", {"query_id": 3810512, "limit": 3})
            d = r.get("result", r)
            results["dune_query"] = d
            print(f"    Rows returned: {d.get('row_count', 0)}")
            print(f"    Columns:       {d.get('columns', [])}")
            print(f"    Source:        {d.get('source')} | {d.get('generated_at','')[:19]}")
            for row in d.get("rows", [])[:3]:
                print(f"      {json.dumps(row, default=str)}")
            _print_live(session, "dune_query", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    SKIPPED — {e}\n")

        summary = session.summary()

    # ── Step 4: Final report ──────────────────────────────────────────────────
    _section("Step 4 — Analysis Report")

    price_data = results.get("token_price", {})
    liq_data   = results.get("dex_liquidity", {})
    gas_data   = results.get("gas_tracker", {})
    whale_data = results.get("whale_activity", {})
    dune_data  = results.get("dune_query", {})

    eth_price     = Decimal(str(price_data.get("price_usd", 0) or 0))
    eth_change    = Decimal(str(price_data.get("change_24h_pct", 0) or 0))
    volume        = Decimal(str(liq_data.get("volume_24h_usd", 0) or 0))
    gas_fast      = gas_data.get("fast_gwei", "?")
    whale_count   = len(whale_data.get("large_transfers", []))
    whale_vol     = Decimal(str(whale_data.get("total_volume_usd", 0) or 0))
    dune_rows     = dune_data.get("row_count", "n/a")

    print(f"""
  ETH @ ${eth_price:,.2f}  ({eth_change:+.4f}% 24h)

  Market:
    24h trading volume  ${volume:>15,.0f}
    Large whale moves   {whale_count} transfers  |  ${whale_vol:>12,.0f} total

  Network:
    Ethereum gas (fast) {gas_fast} gwei  — network {"congested" if Decimal(str(gas_fast or 0)) > 50 else "normal"}

  Onchain (Dune):
    Query 3810512 returned {dune_rows} rows of live data
""")

    # ── Step 5: Cost breakdown ────────────────────────────────────────────────
    _section("Step 5 — Cost Breakdown")
    print(f"\n  {'Tool':<30} {'Cost':>8}   Stellar Tx")
    print(f"  {'─'*30} {'─'*8}   {'─'*20}")
    for entry in summary["breakdown"]:
        tx = (entry.get("tx_hash") or "")[:20]
        label = entry["tool"]
        if "fallback_for" in entry:
            label += f" ← {entry['fallback_for']}"
        print(f"  {label:<30} {_fmt(entry['amount_usdc']):>8}   {tx}...")

    budget_used_pct = Decimal(summary["spent_usdc"]) / Decimal(MAX_BUDGET) * 100
    print(f"\n  {'─'*58}")
    print(f"  Tools called:    {summary['calls']}")
    print(f"  Total spent:     {summary['spent_fmt']}  ({budget_used_pct:.1f}% of ${MAX_BUDGET} budget)")
    print(f"  Remaining:       {summary['remaining_fmt']}")
    print(f"  {'─'*58}\n")

    # Save full results
    out = os.path.join(_here, "budget_demo_result.json")
    with open(out, "w") as f:
        json.dump({"eth_analysis": results, "session": summary}, f, indent=2, default=str)
    print(f"  Full results saved → {out}\n")


if __name__ == "__main__":
    main()
