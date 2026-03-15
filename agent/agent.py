"""
agent.py — AgentPay demo agent using budget-managed sessions.

Demonstrates:
  - Autonomous tool calls with automatic x402 payment
  - Hard budget cap (BudgetExceeded raised if overspent)
  - Live spend tracking printed after every call
  - Automatic fallback to cheaper tools when budget is tight
  - Session summary with per-call cost breakdown

Usage:
    python agent.py

Required in .env:
    TEST_AGENT_SECRET_KEY=S...
    AGENTPAY_GATEWAY_URL=http://localhost:8001  (optional, default)
"""

import os
import sys
import json
import httpx
import logging
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.wallet import AgentWallet, Session, BudgetExceeded

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

GATEWAY_URL   = os.getenv("AGENTPAY_GATEWAY_URL", "http://localhost:8001")
AGENT_SECRET  = os.getenv("TEST_AGENT_SECRET_KEY", "")


# ── AgentPay HTTP Client ──────────────────────────────────────────────────────

class AgentPayClient:
    """
    HTTP client that handles the full x402 payment flow.

    When a tool returns 402:
      1. Reads payment details from response
      2. Sends USDC via Stellar
      3. Retries with payment proof header
    """

    def __init__(self, wallet: AgentWallet, gateway_url: str = GATEWAY_URL):
        self.wallet = wallet
        self.gateway_url = gateway_url
        self.call_log: list[dict] = []

    def call_tool(self, tool_name: str, parameters: dict, max_spend: str = None) -> dict:
        """
        Call a paid tool. Handles 402 automatically.
        Raises ValueError if max_spend is set and price exceeds it.
        """
        url = f"{self.gateway_url}/tools/{tool_name}/call"
        payload = {"parameters": parameters, "agent_address": self.wallet.public_key}

        with httpx.Client(timeout=60.0) as client:

            # ── First request — no payment ─────────────────────────────────
            logger.info(f"→ Calling: {tool_name} | params: {parameters}")
            resp = client.post(url, json=payload)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code != 402:
                raise Exception(f"Unexpected {resp.status_code}: {resp.text}")

            # ── 402 received — parse payment details ───────────────────────
            data = resp.json()
            payment_id  = data["payment_id"]
            amount_usdc = data["amount_usdc"]
            pay_to      = data["pay_to"]

            logger.info(f"  402 — pay {amount_usdc} USDC to {pay_to[:12]}...")

            # Optional per-call spend cap
            if max_spend and float(amount_usdc) > float(max_spend):
                raise ValueError(
                    f"Tool '{tool_name}' costs {amount_usdc} USDC "
                    f"which exceeds max_spend={max_spend}"
                )

            # ── Send payment on Stellar ────────────────────────────────────
            logger.info(f"  Sending payment on Stellar {self.wallet.network}...")
            payment = self.wallet.pay(
                destination=pay_to,
                amount_usdc=amount_usdc,
                memo=payment_id[:28],
            )

            if not payment["success"]:
                raise Exception(f"Payment failed: {payment['reason']}")

            tx_hash = payment["tx_hash"]
            logger.info(f"  ✓ Payment sent | tx: {tx_hash[:16]}...")

            # ── Retry with payment proof ───────────────────────────────────
            proof_header = (
                f"tx_hash={tx_hash},"
                f"from={self.wallet.public_key},"
                f"id={payment_id}"
            )
            retry = client.post(
                url,
                json=payload,
                headers={
                    "X-Payment": proof_header,
                    "X-Agent-Address": self.wallet.public_key,
                },
            )

            if retry.status_code != 200:
                raise Exception(f"Tool call failed after payment: {retry.text}")

            result = retry.json()

            self.call_log.append({
                "tool": tool_name,
                "amount_usdc": amount_usdc,
                "tx_hash": tx_hash,
                "success": True,
            })

            logger.info(f"  ✓ Result received for {tool_name}")
            return result


# ── Demo: Token Analysis Task ─────────────────────────────────────────────────

def _bar(spent: float, budget: float, width: int = 30) -> str:
    """Render a simple ASCII spend bar."""
    filled = int(round((spent / budget) * width)) if budget > 0 else 0
    filled = min(filled, width)
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def analyze_token(token: str, wallet: AgentWallet, max_budget: str = "0.05"):
    """
    Analyze a crypto token using paid tools, with live budget tracking.
    Uses Session for automatic budget enforcement and fallback routing.
    """
    budget_f = float(max_budget)

    print(f"\n{'═'*58}")
    print(f"  AgentPay — Token Analysis")
    print(f"  Token:   {token.upper()}")
    print(f"  Budget:  ${max_budget} USDC")
    print(f"  Wallet:  {wallet.public_key[:20]}...")
    print(f"  Network: {wallet.network}")
    print(f"{'═'*58}\n")

    results = {}

    with Session(wallet=wallet, gateway_url=GATEWAY_URL, max_spend=max_budget) as session:

        # Pre-flight: show estimated costs for all planned tools
        planned = ["token_price", "gas_tracker", "dex_liquidity", "whale_activity"]
        print("  Estimated costs:")
        total_est = 0.0
        for tool in planned:
            est = session.estimate(tool)
            val = float(est.lstrip("$")) if est != "unknown" else 0
            total_est += val
            print(f"    {tool:<22} {est}")
        print(f"    {'─'*30}")
        print(f"    {'Total estimate':<22} ${total_est:.4f}  (budget: ${max_budget})")
        print()

        def _print_budget(label: str, cost: str):
            spent_f  = float(session._spent)
            rem      = session.remaining()
            bar      = _bar(spent_f, budget_f)
            print(f"  {bar}  spent {session.spent()}  remaining {rem}  [{label} cost {cost}]")

        # ── Step 1: Token price ───────────────────────────────────────────
        print(f"  Step 1/4 — token_price ({token.upper()})")
        try:
            r = session.call("token_price", {"symbol": token})
            data = r.get("result", r)
            results["price"] = data
            print(f"    Price:      ${data.get('price_usd', 0):>12,.2f}")
            print(f"    24h change: {data.get('change_24h_pct', 0):>+.4f}%")
            print(f"    Market cap: ${data.get('market_cap_usd', 0):>15,.0f}")
            _print_budget("token_price", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    Skipped — {e}")

        # ── Step 2: Gas tracker ───────────────────────────────────────────
        print(f"\n  Step 2/4 — gas_tracker")
        try:
            r = session.call("gas_tracker", {})
            data = r.get("result", r)
            results["gas"] = data
            print(f"    Slow:       {data.get('slow_gwei')} gwei")
            print(f"    Standard:   {data.get('standard_gwei')} gwei")
            print(f"    Fast:       {data.get('fast_gwei')} gwei")
            print(f"    Base fee:   {data.get('base_fee_gwei')} gwei")
            _print_budget("gas_tracker", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    Skipped — {e}")

        # ── Step 3: DEX liquidity ─────────────────────────────────────────
        print(f"\n  Step 3/4 — dex_liquidity ({token.upper()}/USDC)")
        try:
            r = session.call("dex_liquidity", {"token_a": token, "token_b": "USDC"})
            data = r.get("result", r)
            results["liquidity"] = data
            print(f"    24h volume: ${data.get('volume_24h_usd', 0):>15,.0f}")
            print(f"    Mkt cap:    ${data.get('market_cap_usd', 0):>15,.0f}")
            print(f"    ATH:        ${data.get('ath_usd', 0):>12,.2f}")
            _print_budget("dex_liquidity", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    Skipped — {e}")

        # ── Step 4: Whale activity ────────────────────────────────────────
        print(f"\n  Step 4/4 — whale_activity ({token.upper()})")
        try:
            r = session.call("whale_activity", {"token": token, "min_usd": 100_000})
            data = r.get("result", r)
            results["whales"] = data
            transfers = data.get("large_transfers", data.get("recent_moves", []))
            print(f"    Large transfers detected: {len(transfers)}")
            for m in transfers[:3]:
                val = m.get("usd_value") or m.get("amount_usd", 0)
                print(f"      ${val:>12,.0f}   {m.get('minutes_ago', '?')} min ago")
            _print_budget("whale_activity", session._call_log[-1]["amount_usdc"])
        except BudgetExceeded as e:
            print(f"    Skipped — {e}")

        summary = session.summary()

    return {"token": token, "results": results, "session": summary}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not AGENT_SECRET:
        print("ERROR: TEST_AGENT_SECRET_KEY not set in .env")
        return

    wallet = AgentWallet(
        secret_key=AGENT_SECRET,
        network=os.getenv("STELLAR_NETWORK", "testnet"),
    )

    print(f"Agent wallet: {wallet.public_key}")
    balance = wallet.get_usdc_balance()
    print(f"USDC balance: {balance} USDC")

    if float(balance) < 0.01:
        print("\nWARNING: Low USDC balance.")
        return

    result = analyze_token("ETH", wallet, max_budget="0.05")

    output_file = os.path.join(os.path.dirname(__file__), "analysis_result.json")
    with open(output_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nFull result saved to {output_file}")


if __name__ == "__main__":
    main()
