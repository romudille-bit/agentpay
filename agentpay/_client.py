"""
_client.py — HTTP client that handles the x402 payment flow.

Internal helper used by Session.call(). Not part of the public API.
"""

import httpx
import logging
from decimal import Decimal

from agentpay._wallet import AgentWallet, PaymentFailed

logger = logging.getLogger(__name__)


class AgentPayClient:
    """
    HTTP client that handles the full x402 payment flow.

    When a tool returns 402:
      1. Reads payment details from response
      2. Sends USDC via Stellar
      3. Retries with payment proof header
    """

    def __init__(self, wallet: AgentWallet, gateway_url: str):
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
            if max_spend and Decimal(str(amount_usdc)) > Decimal(str(max_spend)):
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
                raise PaymentFailed(payment["reason"])

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
