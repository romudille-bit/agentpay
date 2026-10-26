"""
stellar_x402.py — the standard x402 `exact` scheme on Stellar.

The payer signs Soroban auth entries for a USDC `transfer` to the gateway;
the facilitator (Built on Stellar / OpenZeppelin) verifies, sponsors the fee
and submits. The gateway signs nothing on this path. The classic
payment+memo rail in gateway/stellar.py is a separate path and unchanged.
See docs/stellar-standard-scheme.md.
"""

import logging
from decimal import Decimal, ROUND_CEILING
from typing import Optional

import httpx

from gateway.config import settings, stellar_caip2
from gateway.services import supabase as sb

logger = logging.getLogger(__name__)

# USDC Stellar Asset Contract ids, the same table @x402/stellar ships.
USDC_SAC = {
    "stellar:pubnet":  "CCW67TSZV3SSS2HXMBQ5JFGCKJNXKZM7UQUWUZPUTHXSTZLEO7SJMI75",
    "stellar:testnet": "CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA",
}
USDC_DECIMALS = 7

# Settled tx hashes this worker has already fulfilled; replay_tx_hashes is
# the durable consume (same pattern as gateway/base.py).
_used_tx_hashes: dict[str, None] = {}
_USED_MAX = 50_000

# Facilitator reasons that mean the payment is definitely not settleable.
_REJECTED_PREFIXES = ("invalid_", "insufficient_", "unsupported_", "unexpected_")


def _remember(tx_hash: str) -> None:
    _used_tx_hashes[tx_hash] = None
    while len(_used_tx_hashes) > _USED_MAX:
        _used_tx_hashes.pop(next(iter(_used_tx_hashes)))


def configured() -> bool:
    return bool(settings.STELLAR_STANDARD_CLIENTS
                and settings.STELLAR_FACILITATOR_API_KEY
                and settings.GATEWAY_PUBLIC_KEY)


def network_label() -> str:
    return f"stellar-{settings.STELLAR_NETWORK}"


def facilitator_url() -> str:
    if settings.STELLAR_NETWORK == "mainnet":
        return settings.STELLAR_FACILITATOR_URL.rstrip("/")
    return settings.STELLAR_FACILITATOR_TESTNET_URL.rstrip("/")


def usdc_atomic(price_usdc) -> str:
    units = (Decimal(str(price_usdc or "0")) * (10 ** USDC_DECIMALS))
    return str(int(units.to_integral_value(rounding=ROUND_CEILING)))


def payment_requirements(price_usdc, resource_url: str, description: str = "") -> dict:
    """The `accepts[]` entry for the 402 and the requirements sent to the
    facilitator — one builder so they cannot drift."""
    caip2 = stellar_caip2()
    return {
        "scheme":            "exact",
        "network":           caip2,
        "amount":            usdc_atomic(price_usdc),
        "asset":             settings.STELLAR_USDC_SAC or USDC_SAC[caip2],
        "payTo":             settings.GATEWAY_PUBLIC_KEY,
        "maxTimeoutSeconds": settings.STELLAR_MAX_TIMEOUT_S,
        "resource":          resource_url,
        "description":       description or "AgentPay tool call",
        "mimeType":          "application/json",
        "extra":             {"areFeesSponsored": True},
    }


def payload_network(payload: dict) -> str:
    acc = payload.get("accepted")
    acc = acc if isinstance(acc, dict) else {}
    return str(acc.get("network") or payload.get("network") or "")


def is_standard_payload(payload: dict) -> bool:
    inner = payload.get("payload")
    return (payload_network(payload).startswith("stellar:")
            and isinstance(inner, dict) and bool(inner.get("transaction")))


async def _post(path: str, payload: dict, requirements: dict) -> tuple[Optional[dict], str]:
    """POST to the facilitator. Returns (json, "") or (None, reason)."""
    headers = {"Authorization": f"Bearer {settings.STELLAR_FACILITATOR_API_KEY}"}
    body = {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirements}
    try:
        async with httpx.AsyncClient(timeout=settings.STELLAR_FACILITATOR_TIMEOUT_S) as client:
            resp = await client.post(f"{facilitator_url()}{path}", json=body, headers=headers)
    except httpx.TimeoutException:
        return None, "facilitator_timeout"
    except Exception as e:
        return None, f"facilitator_unreachable: {str(e)[:120]}"
    if resp.status_code >= 500:
        return None, f"facilitator_http_{resp.status_code}"
    try:
        data = resp.json()
    except Exception:
        return None, f"facilitator_bad_body_http_{resp.status_code}"
    if not isinstance(data, dict):
        return None, f"facilitator_bad_body_http_{resp.status_code}"
    if resp.status_code != 200 and "isValid" not in data and "success" not in data:
        return None, f"facilitator_http_{resp.status_code}"
    return data, ""


async def verify(payload: dict, requirements: dict) -> dict:
    """{"ok": bool, "payer": str, "reason": str, "unavailable": bool}."""
    data, err = await _post("/verify", payload, requirements)
    if data is None:
        return {"ok": False, "payer": "", "reason": err, "unavailable": True}
    payer = str(data.get("payer") or "")
    if data.get("isValid"):
        return {"ok": True, "payer": payer, "reason": "ok", "unavailable": False}
    return {"ok": False, "payer": payer,
            "reason": str(data.get("invalidReason") or "verify_failed"),
            "unavailable": False}


async def settle(payload: dict, requirements: dict) -> dict:
    """{"success", "tx_hash", "payer", "network", "reason", "uncertain"}.

    `uncertain` = the facilitator may have submitted the transaction but we
    got no answer; nothing is consumed and the caller must not re-sign.
    """
    data, err = await _post("/settle", payload, requirements)
    if data is None:
        uncertain = err == "facilitator_timeout" or err.startswith("facilitator_http_5")
        return {"success": False, "tx_hash": "", "payer": "", "network": "",
                "reason": err, "uncertain": uncertain}
    tx = str(data.get("transaction") or "")
    payer = str(data.get("payer") or "")
    network = str(data.get("network") or requirements.get("network") or "")
    if not data.get("success"):
        reason = str(data.get("errorReason") or data.get("invalidReason") or "settle_failed")
        return {"success": False, "tx_hash": tx, "payer": payer, "network": network,
                "reason": reason, "uncertain": not reason.startswith(_REJECTED_PREFIXES)}

    if len(tx) != 64 or any(c not in "0123456789abcdefABCDEF" for c in tx):
        return {"success": False, "tx_hash": tx, "payer": payer, "network": network,
                "reason": "facilitator_malformed_transaction", "uncertain": True}
    if not payer.startswith(("G", "C")) or len(payer) != 56:
        return {"success": False, "tx_hash": tx, "payer": payer, "network": network,
                "reason": "facilitator_malformed_payer", "uncertain": True}

    # Atomic consume of the settled hash, same contract as the Base path.
    if tx in _used_tx_hashes:
        return {"success": False, "tx_hash": tx, "payer": payer, "network": network,
                "reason": "replay_attack", "uncertain": False}
    _remember(tx)
    recorded = await sb.record_tx_hash(tx, network_label())
    if recorded is False:
        return {"success": False, "tx_hash": tx, "payer": payer, "network": network,
                "reason": "replay_attack", "uncertain": False}
    if recorded is None:
        _used_tx_hashes.pop(tx, None)
        return {"success": False, "tx_hash": tx, "payer": payer, "network": network,
                "reason": ("replay_check_unavailable: durable replay store "
                           "unreachable — retry the same proof"), "uncertain": True}
    return {"success": True, "tx_hash": tx, "payer": payer, "network": network,
            "reason": "ok", "uncertain": False}
