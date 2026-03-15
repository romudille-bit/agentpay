"""
agent.py — AgentPay demo agent using budget-managed sessions.

Demonstrates:
  - Autonomous tool calls with automatic x402 payment
  - Hard budget cap (BudgetExceeded raised if overspent)
  - Session summary with per-call cost breakdown
  - Clean one-liner SDK interface via agentpay.Session()

Usage:
    python agent.py

Required in .env:
    TEST_AGENT_SECRET_KEY=S...
    AGENTPAY_GATEWAY_URL=http://localhost:8000  (optional, default)
"""

import os
import sys
import json
import httpx
import logging
from dotenv import load_dotenv

load_dotenv("../.env")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.wallet import AgentWallet, BudgetSession, BudgetExceeded

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

GATEWAY_URL = os.getenv("AGENTPAY_GATEWAY_URL", "http://localhost:8000")
AGENT_SECRET = os.getenv("TEST_AGENT_SECRET_KEY", "")


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

def analyze_token(token: str, wallet: AgentWallet, max_budget: str = "0.05"):
    """
    Analyze a crypto token using 4 paid tools, within a budget cap.
    This is the core AgentPay demo.
    """
    print(f"\n{'═'*54}")
    print(f"  AgentPay Demo — Token Analysis")
    print(f"  Token:   {token.upper()}")
    print(f"  Budget:  {max_budget} USDC max")
    print(f"  Wallet:  {wallet.public_key[:16]}...")
    print(f"  Network: {wallet.network}")
    print(f"{'═'*54}\n")

    results = {}

    with BudgetSession(wallet=wallet, gateway_url=GATEWAY_URL, max_spend=max_budget) as session:

        # Pre-flight: show estimated costs
        print("Estimated costs:")
        for tool in ["token_price", "dex_liquidity", "whale_activity", "gas_tracker"]:
            price = session.estimate(tool)
            print(f"  {tool:<22} ~{price} USDC")
        print()

        # Step 1: Token price
        print("Step 1/4 — token_price")
        try:
            r = session.call("token_price", {"symbol": token})
            results["price"] = r.get("result", r)
            print(f"  Price: ${results['price'].get('price_usd', 0):,.2f}")
        except BudgetExceeded as e:
            print(f"  Skipped — {e}")

        # Step 2: DEX liquidity
        print("\nStep 2/4 — dex_liquidity")
        try:
            r = session.call("dex_liquidity", {"token_a": token, "token_b": "USDC"})
            results["liquidity"] = r.get("result", r)
            liq = results["liquidity"]
            print(f"  Liquidity: ${liq.get('liquidity_usd', 0):,.0f}")
            print(f"  24h Volume: ${liq.get('volume_24h_usd', 0):,.0f}")
        except BudgetExceeded as e:
            print(f"  Skipped — {e}")

        # Step 3: Whale activity
        print("\nStep 3/4 — whale_activity")
        try:
            r = session.call("whale_activity", {"token": token, "min_usd": 100000})
            results["whales"] = r.get("result", r)
            moves = results["whales"].get("recent_moves", [])
            print(f"  Large moves detected: {len(moves)}")
            for m in moves[:2]:
                print(f"    ${m.get('amount_usd', 0):,.0f}  —  {m.get('minutes_ago')} min ago")
        except BudgetExceeded as e:
            print(f"  Skipped — {e}")

        # Step 4: Gas tracker
        print("\nStep 4/4 — gas_tracker")
        try:
            r = session.call("gas_tracker", {})
            results["gas"] = r.get("result", r)
            gas = results["gas"]
            print(f"  Standard: {gas.get('standard_gwei')} gwei")
            print(f"  Fast:     {gas.get('fast_gwei')} gwei")
        except BudgetExceeded as e:
            print(f"  Skipped — {e}")

        # Session summary prints automatically on __exit__
        summary = session.summary()

    return {"token": token, "results": results, "session": summary}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not AGENT_SECRET:
        print("ERROR: TEST_AGENT_SECRET_KEY not set in .env")
        print("Run `python setup_wallet.py` first.")
        return

    wallet = AgentWallet(
        secret_key=AGENT_SECRET,
        network=os.getenv("STELLAR_NETWORK", "testnet"),
    )

    print(f"Agent wallet: {wallet.public_key}")
    balance = wallet.get_usdc_balance()
    print(f"USDC balance: {balance} USDC")

    if float(balance) < 0.01:
        print("\nWARNING: Low USDC balance. Get testnet USDC before running.")
        print("See README.md → Step 5 for instructions.")
        return

    result = analyze_token("ETH", wallet, max_budget="0.05")

    output_file = "analysis_result.json"
    with open(output_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nFull result saved to {output_file}")


if __name__ == "__main__":
    main()
