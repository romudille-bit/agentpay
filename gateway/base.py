"""
base.py — Base/EVM payment verification via Coinbase CDP x402 facilitator.

Flow:
  1. Gateway issues 402 with PaymentRequirements (Base option)
  2. Client signs EIP-3009 transferWithAuthorization off-chain
  3. Client sends signed payload in PAYMENT-SIGNATURE header (base64 JSON)
  4. Gateway calls POST https://x402.coinbase.com/settle
     → CDP submits on-chain tx, returns tx hash
  5. Gateway executes tool, returns result
"""

import base64
import json
import logging
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

FACILITATOR_URL = "https://x402.coinbase.com"

# USDC contract addresses
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_BASE_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# CAIP-2 chain identifiers
CAIP2_BASE_SEPOLIA = "eip155:84532"
CAIP2_BASE_MAINNET = "eip155:8453"


def get_chain_config(network: str) -> tuple[str, str]:
    """Return (caip2_chain_id, usdc_contract) for a network name."""
    if network in ("base", "base-mainnet"):
        return CAIP2_BASE_MAINNET, USDC_BASE_MAINNET
    # default: base-sepolia
    return CAIP2_BASE_SEPOLIA, USDC_BASE_SEPOLIA


def usdc_to_atomic(amount_usdc: str) -> str:
    """Convert USDC decimal string to atomic integer string (6 decimals).
    '0.001' → '1000'  |  '0.002' → '2000'  |  '1.0' → '1000000'
    """
    return str(int(Decimal(amount_usdc) * Decimal("1000000")))


def build_payment_requirements(
    amount_usdc: str,
    pay_to: str,
    resource_url: str,
    network: str = "base-sepolia",
) -> dict:
    """
    Build the PaymentRequirements object used in both the 402 body and
    the /settle request to the CDP facilitator.
    """
    caip2, usdc_contract = get_chain_config(network)
    return {
        "scheme":            "exact",
        "network":           caip2,
        "amount":            usdc_to_atomic(amount_usdc),
        "asset":             usdc_contract,
        "payTo":             pay_to,
        "maxTimeoutSeconds": 300,
        "resource":          resource_url,
        "description":       "AgentPay tool call",
        "mimeType":          "application/json",
        "extra": {
            "name":                "USDC",
            "version":             "2",
            "assetTransferMethod": "eip3009",   # preferred for USDC on Base
        },
    }


def build_payment_required_header(
    requirements: dict,
    resource_url: str,
    tool_description: str = "",
) -> str:
    """
    Build the PAYMENT-REQUIRED header value (base64-encoded x402 v2 JSON).
    Sent alongside the 402 response for x402-v2-aware clients.
    """
    payload = {
        "x402Version": 2,
        "error":        "Payment required",
        "resource": {
            "url":         resource_url,
            "description": tool_description,
            "mimeType":    "application/json",
        },
        "accepts": [requirements],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _decode_payment_signature(header: str) -> tuple[dict | None, str]:
    """
    Decode the PAYMENT-SIGNATURE header (base64 JSON).
    Returns (payload_dict, error_message). On success error_message is "".
    """
    # Add padding if needed
    padded = header + "=" * (-len(header) % 4)
    try:
        return json.loads(base64.b64decode(padded)), ""
    except Exception as e:
        return None, f"Invalid PAYMENT-SIGNATURE encoding: {e}"


async def settle_base_payment(
    payment_signature_header: str,
    payment_requirements: dict,
) -> dict:
    """
    Verify and settle a Base/EVM payment via the CDP x402 facilitator.

    The CDP /settle endpoint atomically verifies the EIP-3009 authorization
    and submits the on-chain transferWithAuthorization transaction.

    Returns:
        {
          "success": bool,
          "tx_hash": str,   # EVM transaction hash (0x...)
          "payer":   str,   # EVM address of payer
          "network": str,   # CAIP-2 network identifier
          "reason":  str,   # "ok" on success, error code on failure
        }
    """
    payload, err = _decode_payment_signature(payment_signature_header)
    if err:
        return {"success": False, "tx_hash": "", "payer": "", "network": "", "reason": err}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{FACILITATOR_URL}/settle",
                json={
                    "x402Version":        2,
                    "paymentPayload":     payload,
                    "paymentRequirements": payment_requirements,
                },
                headers={"Content-Type": "application/json"},
            )
    except Exception as e:
        return {"success": False, "tx_hash": "", "payer": "", "network": "", "reason": f"Facilitator unreachable: {e}"}

    if resp.status_code != 200:
        logger.warning(f"[BASE] Facilitator HTTP {resp.status_code}: {resp.text[:200]}")
        return {
            "success": False,
            "tx_hash": "",
            "payer":   "",
            "network": "",
            "reason":  f"facilitator_http_{resp.status_code}",
        }

    data = resp.json()
    logger.info(f"[BASE] Settle response: success={data.get('success')} tx={data.get('transaction','')[:20]}...")

    if data.get("success"):
        return {
            "success": True,
            "tx_hash": data.get("transaction", ""),
            "payer":   data.get("payer", ""),
            "network": data.get("network", ""),
            "reason":  "ok",
        }

    return {
        "success": False,
        "tx_hash": data.get("transaction", ""),
        "payer":   data.get("payer", ""),
        "network": data.get("network", ""),
        "reason":  data.get("errorReason", "settle_failed"),
    }
