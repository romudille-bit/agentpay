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

import asyncio
import uuid
import time
import hashlib
import json
from typing import Optional
from dataclasses import dataclass, asdict

from gateway.stellar import verify_payment, split_payment
from gateway.config import settings
from gateway.services import supabase as sb
import logging

logger = logging.getLogger(__name__)


def _fire_and_forget(coro) -> None:
    """Schedule an async coroutine without awaiting it, no-op if no event loop.

    Used for fire-and-forget Supabase dual-writes. issue_payment_challenge is
    synchronous but called from inside async FastAPI handlers in production,
    so a running event loop is normally available. In test contexts (e.g.
    test_x402.py calls issue_payment_challenge synchronously) there's no
    loop — `RuntimeError: no running event loop` is the expected signal to
    skip the dual-write. The coroutine is closed to suppress the
    "coroutine was never awaited" warning.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    loop.create_task(coro)

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

    # Dual-write to Supabase (fire-and-forget). In-memory dict is still
    # source of truth in this PR; Supabase becomes primary at #13 cutover.
    _fire_and_forget(
        sb.store_pending_challenge(
            payment_id=payment_id,
            tool_name=tool_name,
            amount_usdc=price_usdc,
            gateway_address=settings.GATEWAY_PUBLIC_KEY,
            developer_address=developer_address,
            expires_at=challenge.expires_at,
            request_data=request_data,
        )
    )
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

    Both tx_hash and id must be present and non-empty for the parse to
    succeed — empty values previously slipped through and got added to
    _completed_payments as the empty string.
    """
    if not header_value:
        return None
    try:
        result = {}
        for part in header_value.split(","):
            key, _, val = part.partition("=")
            result[key.strip()] = val.strip()
        return result if result.get("tx_hash") and result.get("id") else None
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

    # Dual-write replay state to Supabase (fire-and-forget). The in-memory
    # set + dict are still primary; these calls just shadow them so they
    # survive a Railway redeploy. record_tx_hash uses the composite PK
    # (tx_hash, network) to keep stellar-mainnet and stellar-testnet
    # independent. delete_pending_challenge removes the row we just
    # consumed so cleanup_expired_challenges() doesn't have to.
    network_label = f"stellar-{settings.STELLAR_NETWORK}"
    asyncio.create_task(sb.record_payment_id(payment_id))
    asyncio.create_task(sb.record_tx_hash(tx_hash, network_label))
    asyncio.create_task(sb.delete_pending_challenge(payment_id))

    # Trigger revenue split (async, non-blocking).
    # In production: queue this as a background job.
    #
    # Skip the split when developer_address == gateway wallet. For
    # AgentPay-owned tools this is always the case, and a self-send would
    # only burn a tx fee. On testnet the mainnet dev address also doesn't
    # exist, so the split would fail every time and spam the logs.
    developer_address = challenge_data.get("developer_address") or ""
    if developer_address and developer_address != settings.GATEWAY_PUBLIC_KEY:
        # asyncio is imported at module level; do NOT add a local `import
        # asyncio` here. Doing so binds `asyncio` as a function-local for
        # the entire function body, which shadows the module import and
        # raises UnboundLocalError at the earlier asyncio.create_task lines
        # above (replay dual-writes). The deploy of PR #13b crashed every
        # paid call until this was fixed. The TestVerifyAndFulfill class
        # in tests/test_x402.py is the regression guard.
        asyncio.create_task(
            split_payment(
                tool_developer_address=developer_address,
                total_amount_usdc=challenge_data["amount_usdc"],
                gateway_fee_percent=settings.GATEWAY_FEE_PERCENT,
            )
        )
    else:
        logger.info(
            f"Skipping revenue split — developer == gateway "
            f"({developer_address[:10] + '...' if developer_address else 'unset'})"
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
