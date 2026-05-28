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

import asyncio
import base64
import json
import logging
import secrets
import time
from decimal import Decimal

import httpx
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from gateway.services import supabase as sb

logger = logging.getLogger(__name__)

# Coinbase CDP x402 facilitator — required for Base Bazaar auto-indexing.
# When a payment flows through /settle here, Bazaar reads the resource_url
# from paymentRequirements and indexes the tool at that URL automatically.
# Old URL (still works but not Bazaar-aware): https://x402.coinbase.com
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"

# Legacy alias — kept so existing callers that reference FACILITATOR_URL
# don't break. Remove once all internal callers are updated.
FACILITATOR_URL = CDP_FACILITATOR_URL

# In-memory replay protection for Base tx hashes
_used_base_tx_hashes: set[str] = set()

# USDC contract addresses
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_BASE_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# CAIP-2 chain identifiers
CAIP2_BASE_SEPOLIA = "eip155:84532"
CAIP2_BASE_MAINNET = "eip155:8453"


# Maps a CAIP-2 network id to the canonical short label that Supabase's
# replay_tx_hashes table uses (composite PK on (tx_hash, network)). The
# plan settled on these four constants — keep them in sync if a new chain
# is added.
_CAIP2_TO_NETWORK_LABEL = {
    CAIP2_BASE_MAINNET: "base-mainnet",
    CAIP2_BASE_SEPOLIA: "base-sepolia",
}


def _network_label(caip2: str) -> str:
    """Translate a CAIP-2 chain id to the short label used in Supabase.

    Falls back to the CAIP-2 string itself if unknown so we never drop a
    record_tx_hash call due to a missing mapping — better to have a row
    keyed on "eip155:1234" than no row at all.
    """
    return _CAIP2_TO_NETWORK_LABEL.get(caip2, caip2)


def _build_cdp_jwt(key_name: str, key_secret_raw: str, uri: str) -> str:
    """Build a signed ES256 JWT for Coinbase CDP API authentication.

    CDP expects:
      Authorization: Bearer <jwt>

    where the JWT is signed with the EC private key from the CDP portal.

    Args:
        key_name:       CDP key name, e.g. "organizations/abc.../apiKeys/xyz..."
        key_secret_raw: PEM private key — Railway stores it with literal \\n;
                        we convert those back to real newlines before parsing.
        uri:            Request URI in CDP format: "METHOD hostname/path"
                        e.g. "POST api.cdp.coinbase.com/platform/v2/x402/settle"
    """
    # Railway env vars can't store literal newlines — the PEM key arrives with
    # \\n (backslash-n string). Convert to real newlines before loading.
    pem = key_secret_raw.replace("\\n", "\n").encode()
    private_key = load_pem_private_key(pem, password=None)

    now = int(time.time())
    payload = {
        "sub":   key_name,
        "iss":   "cdp",
        "nbf":   now,
        "exp":   now + 120,
        "uri":   uri,
    }
    headers = {
        "kid":   key_name,
        "nonce": secrets.token_hex(16),
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


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
    output_schema: dict | None = None,
) -> str:
    """
    Build the PAYMENT-REQUIRED header value (base64-encoded x402 v2 JSON).
    Sent alongside the 402 response for x402-v2-aware clients.

    If `output_schema` is provided it's embedded inside the `accepts[0]`
    entry under the `outputSchema` key. This is what Coinbase's Bazaar
    directory reads on the first Base mainnet payment through the CDP
    facilitator to auto-index the tool — without it, the listing shows
    a price but no input/output shape and ranks poorly.

    Conventional shape:
        output_schema = {"input": <JSON Schema of parameters>,
                         "output": <example or schema of response>}
    """
    accepts_entry = dict(requirements)  # shallow copy — don't mutate caller's dict
    if output_schema is not None:
        accepts_entry["outputSchema"] = output_schema
    payload = {
        "x402Version": 2,
        "error":        "Payment required",
        "resource": {
            "url":         resource_url,
            "description": tool_description,
            "mimeType":    "application/json",
        },
        "accepts": [accepts_entry],
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

        # Replay check — Supabase primary, in-memory fallback (PR #13e
        # cutover). Composite PK (tx_hash, network) means the same hash
        # on base-mainnet vs base-sepolia is treated as independent;
        # _used_base_tx_hashes has no network discriminator, so the
        # in-memory fallback is slightly less precise but only matters
        # if Supabase is unreachable AND we're switching networks
        # mid-flight, which doesn't happen in practice.
        caip2 = payment_requirements.get("network", "")
        network_label = _network_label(caip2)
        is_replay = False
        if sb.sb_enabled():
            is_replay = await sb.is_tx_hash_consumed(tx_hash, network_label)
        if not is_replay and tx_hash in _used_base_tx_hashes:
            is_replay = True
        if is_replay:
            return {"success": False, "tx_hash": tx_hash, "payer": payer, "network": "", "reason": "replay_attack"}

        result = await verify_base_tx(
            tx_hash              = tx_hash,
            payer                = payer,
            required_amount_atomic = int(payment_requirements.get("amount", "0")),
            pay_to               = payment_requirements.get("payTo", ""),
            rpc_url              = rpc_url,
        )
        if result["success"]:
            _used_base_tx_hashes.add(tx_hash)
            # Persist replay state to Supabase (primary after PR #13e
            # cutover). Still fire-and-forget — response doesn't wait on
            # the round-trip.
            asyncio.create_task(sb.record_tx_hash(tx_hash, network_label))
        # Inject CAIP-2 network from requirements so callers don't see ""
        result["network"] = payment_requirements.get("network", "")
        return result

    # ── Mode A: CDP Facilitator ───────────────────────────────────────────────
    # Call https://api.cdp.coinbase.com/platform/v2/x402/settle.
    # The CDP facilitator submits the EIP-3009 signed tx on-chain and returns
    # the tx hash. Bazaar reads paymentRequirements.resource on settlement and
    # auto-indexes that URL — this is what makes AgentPay discoverable on Bazaar.
    #
    # Auth: CDP requires a signed ES256 JWT (not a plain bearer token).
    # Set CDP_KEY_NAME + CDP_KEY_SECRET in Railway env vars to enable.
    from gateway.config import settings as _settings
    cdp_headers: dict[str, str] = {"Content-Type": "application/json"}
    if _settings.CDP_KEY_NAME and _settings.CDP_KEY_SECRET:
        settle_uri = f"POST {CDP_FACILITATOR_URL.removeprefix('https://')}/settle"
        try:
            token = _build_cdp_jwt(_settings.CDP_KEY_NAME, _settings.CDP_KEY_SECRET, settle_uri)
            cdp_headers["Authorization"] = f"Bearer {token}"
        except Exception as e:
            logger.warning(f"[BASE] CDP JWT build failed: {e} — proceeding unauthenticated")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{CDP_FACILITATOR_URL}/settle",
                json={
                    "x402Version":        2,
                    "paymentPayload":     payload,
                    "paymentRequirements": payment_requirements,
                },
                headers=cdp_headers,
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
        # Schema-validate the CDP response before trusting it (Tier 2 #19).
        # CDP has occasionally returned {"success": True} with missing or
        # malformed transaction/payer/network fields — silently propagating
        # empty values produced misleading downstream receipts (paid call
        # appears successful but the on-chain proof is unrecoverable).
        # Validate explicit shape before declaring victory.
        tx      = data.get("transaction", "")
        payer   = data.get("payer", "")
        network = data.get("network", "")

        # EVM tx hash: 0x-prefixed, 66-char (0x + 64 hex)
        if not (isinstance(tx, str) and tx.startswith("0x") and len(tx) == 66):
            logger.warning(f"[BASE] CDP success=true but malformed transaction: {tx!r}")
            return {
                "success": False, "tx_hash": tx, "payer": payer, "network": network,
                "reason": "cdp_malformed_transaction",
            }
        # EVM address: 0x-prefixed, 42-char (0x + 40 hex)
        if not (isinstance(payer, str) and payer.startswith("0x") and len(payer) == 42):
            logger.warning(f"[BASE] CDP success=true but malformed payer: {payer!r}")
            return {
                "success": False, "tx_hash": tx, "payer": payer, "network": network,
                "reason": "cdp_malformed_payer",
            }
        # CAIP-2 network identifier (e.g. "eip155:8453") — required, non-empty
        if not (isinstance(network, str) and network):
            logger.warning(f"[BASE] CDP success=true but missing network field")
            return {
                "success": False, "tx_hash": tx, "payer": payer, "network": "",
                "reason": "cdp_missing_network",
            }

        return {
            "success": True,
            "tx_hash": tx, "payer": payer, "network": network,
            "reason":  "ok",
        }

    return {
        "success": False,
        "tx_hash": data.get("transaction", ""),
        "payer":   data.get("payer", ""),
        "network": data.get("network", ""),
        "reason":  data.get("errorReason", "settle_failed"),
    }
