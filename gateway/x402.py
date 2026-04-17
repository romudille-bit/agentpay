"""
x402.py — x402 payment protocol handler for AgentPay.

The x402 protocol works like this:
  1. Client makes a request to a paid endpoint
  2. Server returns HTTP 402 with payment details
  3. Client pays and retries with payment proof in header
  4. Server verifies and fulfills the request

This module handles steps 2 and 4.

x402 Payment-Required response format:
  HTTP 402
  X-Payment-Required: version=1,network=stellar,address=G...,amount=0.001,asset=USDC,id=uuid

Client sends payment proof as:
  X-Payment: tx_hash=...,from=G...,id=uuid
"""

import uuid
import time
import hashlib
import json
from typing import Optional
from dataclasses import dataclass, asdict

from stellar import verify_payment, split_payment
from config import settings
import logging

logger = logging.getLogger(__name__)

# In-memory store of pending payment challenges
# In production: use Redis or Supabase
_pending_challenges: dict[str, dict] = {}
_completed_payments: set[str] = set()  # prevent replay attacks


@dataclass
class PaymentChallenge:
    """A payment challenge issued to an agent."""
    payment_id: str          # Unique ID for this payment request
    tool_name: str           # Which tool they're trying to call
    amount_usdc: str         # How much they need to pay
    gateway_address: str     # Where to send payment
    developer_address: str   # Tool developer wallet (for split)
    issued_at: float         # Unix timestamp
    expires_at: float        # Unix timestamp
    request_data: dict       # Original request (to replay after payment)


def issue_payment_challenge(
    tool_name: str,
    price_usdc: str,
    developer_address: str,
    request_data: dict,
    ttl_seconds: int = 120,
) -> PaymentChallenge:
    """
    Create a payment challenge for an agent to fulfill.
    Called when an agent hits a paid endpoint without payment.
    """
    payment_id = str(uuid.uuid4())
    now = time.time()

    challenge = PaymentChallenge(
        payment_id=payment_id,
        tool_name=tool_name,
        amount_usdc=price_usdc,
        gateway_address=settings.GATEWAY_PUBLIC_KEY,
        developer_address=developer_address,
        issued_at=now,
        expires_at=now + ttl_seconds,
        request_data=request_data,
    )

    _pending_challenges[payment_id] = asdict(challenge)
    logger.info(f"Issued challenge {payment_id} for {tool_name} @ {price_usdc} USDC")
    return challenge


def build_402_headers(challenge: PaymentChallenge) -> dict:
    """
    Build the HTTP headers for a 402 Payment Required response.
    The agent reads these to know where and how much to pay.
    """
    return {
        "X-Payment-Required": (
            f"version=1"
            f",network=stellar"
            f",address={challenge.gateway_address}"
            f",amount={challenge.amount_usdc}"
            f",asset=USDC"
            f",id={challenge.payment_id}"
        ),
        "X-Payment-Expires": str(int(challenge.expires_at)),
        "Content-Type": "application/json",
    }


def parse_payment_header(header_value: str) -> Optional[dict]:
    """
    Parse the X-Payment header sent by the agent after paying.

    Format: tx_hash=abc123,from=GABC...,id=uuid
    """
    if not header_value:
        return None
    try:
        result = {}
        for part in header_value.split(","):
            key, _, val = part.partition("=")
            result[key.strip()] = val.strip()
        return result if "tx_hash" in result and "id" in result else None
    except Exception:
        return None


async def verify_and_fulfill(
    payment_header: str,
    agent_address: str,
) -> dict:
    """
    Verify a payment and authorize tool execution.

    Returns:
        {"authorized": True, "challenge": {...}} on success
        {"authorized": False, "reason": "..."} on failure
    """
    parsed = parse_payment_header(payment_header)
    if not parsed:
        return {"authorized": False, "reason": "Invalid X-Payment header format"}

    payment_id = parsed.get("id")
    tx_hash = parsed.get("tx_hash")

    # Look up challenge
    challenge_data = _pending_challenges.get(payment_id)
    if not challenge_data:
        return {"authorized": False, "reason": "Payment ID not found or expired"}

    # Check expiry
    if time.time() > challenge_data["expires_at"]:
        del _pending_challenges[payment_id]
        return {"authorized": False, "reason": "Payment challenge expired"}

    # Prevent replay attacks
    if tx_hash in _completed_payments:
        return {"authorized": False, "reason": "Payment already used (replay attack)"}

    # Verify payment on Stellar
    result = await verify_payment(
        from_address=agent_address,
        to_address=challenge_data["gateway_address"],
        amount_usdc=challenge_data["amount_usdc"],
        payment_id=payment_id,
        tx_hash=tx_hash or "",
    )

    if not result["verified"]:
        return {"authorized": False, "reason": result["reason"]}

    # Mark as used
    _completed_payments.add(tx_hash)
    del _pending_challenges[payment_id]

    # Trigger revenue split (async, non-blocking)
    # In production: queue this as a background job
    import asyncio
    asyncio.create_task(
        split_payment(
            tool_developer_address=challenge_data["developer_address"],
            total_amount_usdc=challenge_data["amount_usdc"],
            gateway_fee_percent=settings.GATEWAY_FEE_PERCENT,
        )
    )

    logger.info(f"Payment {payment_id} verified. Tool: {challenge_data['tool_name']}")
    return {
        "authorized": True,
        "challenge": challenge_data,
        "tx_hash": tx_hash,
    }


def get_pending_count() -> int:
    """How many open payment challenges exist right now."""
    # Clean expired ones
    now = time.time()
    expired = [k for k, v in _pending_challenges.items() if v["expires_at"] < now]
    for k in expired:
        del _pending_challenges[k]
    return len(_pending_challenges)
