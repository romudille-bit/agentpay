"""
base.py — Base/EVM payment verification for AgentPay.

Two settlement modes (detected automatically from PAYMENT-SIGNATURE content):

  Mode A — CDP facilitator (when CDP is live):
    Client signs EIP-3009 off-chain → sends payload in PAYMENT-SIGNATURE
    → gateway calls POST https://x402.coinbase.com/settle
    → CDP submits on-chain tx

  Mode B — Direct on-chain (current default; no CDP dependency):
    Client calls transferWithAuthorization on USDC contract directly
    → sends {"tx_hash": "0x...", "payer": "0x..."} in PAYMENT-SIGNATURE
    → gateway verifies the tx receipt via JSON-RPC
    → checks Transfer event: from=payer, to=gateway, value>=required

The gateway auto-detects the mode: if payload contains "tx_hash" → Mode B,
if it contains "payload" with an EIP-3009 signature → Mode A (CDP).
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


async def verify_base_tx(
    tx_hash: str,
    payer: str,
    required_amount_atomic: int,
    pay_to: str,
    rpc_url: str,
) -> dict:
    """
    Mode B settlement: verify an already-broadcast USDC transferWithAuthorization.

    Calls eth_getTransactionReceipt via JSON-RPC and checks that:
      - tx succeeded (status == 0x1)
      - USDC contract emitted a Transfer(from=payer, to=pay_to, value>=required)

    Returns same shape as settle_base_payment():
        {"success": bool, "tx_hash": str, "payer": str, "network": str, "reason": str}
    """
    # ERC-20 Transfer(address indexed from, address indexed to, uint256 value)
    TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]},
                headers={"Content-Type": "application/json"},
            )
    except Exception as e:
        return {"success": False, "tx_hash": tx_hash, "payer": payer, "network": "", "reason": f"rpc_unreachable: {e}"}

    if resp.status_code != 200:
        return {"success": False, "tx_hash": tx_hash, "payer": payer, "network": "", "reason": f"rpc_http_{resp.status_code}"}

    data = resp.json()
    receipt = data.get("result")
    if receipt is None:
        return {"success": False, "tx_hash": tx_hash, "payer": payer, "network": "", "reason": "tx_not_found_or_pending"}

    if receipt.get("status") != "0x1":
        return {"success": False, "tx_hash": tx_hash, "payer": payer, "network": "", "reason": "tx_reverted"}

    # Scan logs for USDC Transfer event matching our requirements
    payer_padded   = "0x" + payer.lower().lstrip("0x").zfill(64)
    pay_to_padded  = "0x" + pay_to.lower().lstrip("0x").zfill(64)

    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        if topics[0].lower() != TRANSFER_SIG:
            continue
        if topics[1].lower() != payer_padded:
            continue
        if topics[2].lower() != pay_to_padded:
            continue
        # Decode value from data field (uint256, 32 bytes = 64 hex chars)
        raw_data = log.get("data", "0x").lstrip("0x")
        transferred = int(raw_data.zfill(64), 16)
        if transferred < required_amount_atomic:
            return {
                "success": False, "tx_hash": tx_hash, "payer": payer, "network": "",
                "reason": f"insufficient_transfer: got {transferred}, need {required_amount_atomic}",
            }
        logger.info(f"[BASE] On-chain tx verified: {tx_hash[:20]}... transferred={transferred}")
        return {"success": True, "tx_hash": tx_hash, "payer": payer, "network": "", "reason": "ok"}

    return {"success": False, "tx_hash": tx_hash, "payer": payer, "network": "", "reason": "no_matching_transfer_event"}


async def settle_base_payment(
    payment_signature_header: str,
    payment_requirements: dict,
    rpc_url: str = "",
) -> dict:
    """
    Verify and settle a Base/EVM payment.

    Auto-detects settlement mode from PAYMENT-SIGNATURE content:
      - Mode B (direct): payload has "tx_hash" → verify on-chain via JSON-RPC
      - Mode A (CDP):    payload has "payload"  → call CDP /settle

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

    # ── Mode B: direct on-chain tx ────────────────────────────────────────────
    if "tx_hash" in payload:
        tx_hash = payload.get("tx_hash", "")
        payer   = payload.get("payer", "")
        if not tx_hash or not payer:
            return {"success": False, "tx_hash": "", "payer": "", "network": "", "reason": "tx_hash_or_payer_missing"}
        rpc = rpc_url or "https://sepolia.base.org"
        return await verify_base_tx(
            tx_hash              = tx_hash,
            payer                = payer,
            required_amount_atomic = int(payment_requirements.get("amount", "0")),
            pay_to               = payment_requirements.get("payTo", ""),
            rpc_url              = rpc,
        )

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
