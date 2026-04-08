"""
stellar.py — Stellar wallet utilities for AgentPay gateway.

Handles:
- Payment verification (did the agent actually pay?)
- Splitting payments to tool developers
- Wallet balance checks
"""

import asyncio
import httpx
from decimal import Decimal
from stellar_sdk import (
    Keypair, Network, Server, Asset, TransactionBuilder,
    exceptions as stellar_exceptions
)
from config import settings
import logging

logger = logging.getLogger(__name__)

# ── Network Setup ─────────────────────────────────────────────────────────────

def get_server() -> Server:
    if settings.STELLAR_NETWORK == "testnet":
        return Server("https://horizon-testnet.stellar.org")
    return Server("https://horizon.stellar.org")

def get_network_passphrase() -> str:
    if settings.STELLAR_NETWORK == "testnet":
        return Network.TESTNET_NETWORK_PASSPHRASE
    return Network.PUBLIC_NETWORK_PASSPHRASE

def get_usdc_asset() -> Asset:
    issuer = (
        settings.USDC_ISSUER_TESTNET
        if settings.STELLAR_NETWORK == "testnet"
        else settings.USDC_ISSUER_MAINNET
    )
    return Asset("USDC", issuer)


# ── Payment Verification ──────────────────────────────────────────────────────

async def verify_payment(
    from_address: str,
    to_address: str,
    amount_usdc: str,
    payment_id: str,
    max_age_seconds: int = 60
) -> dict:
    """
    Verify a USDC payment via the OpenZeppelin x402 facilitator.

    The OZ facilitator covers XLM network fees — agents only need USDC,
    no XLM required. Exposes standard /verify, /settle, /supported endpoints.

    Facilitator endpoints:
      mainnet: https://channels.openzeppelin.com/x402
      testnet: https://channels.openzeppelin.com/x402/testnet

    Returns:
        {"verified": True, "tx_hash": "..."} on success
        {"verified": False, "reason": "..."} on failure
    """
    facilitator_url = settings.STELLAR_FACILITATOR_URL

    payload = {
        "x402Version": 1,
        "payload": {
            "from": from_address,
            "to": to_address,
            "amount": amount_usdc,
            "paymentId": payment_id,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{facilitator_url}/verify",
                json=payload
            )
            data = resp.json()
            if data.get("isValid"):
                tx_hash = data.get("txHash", "")
                logger.info(f"Payment verified via OZ facilitator: {tx_hash}")
                return {"verified": True, "tx_hash": tx_hash}
            reason = data.get("invalidReason", "Facilitator rejected payment")
            logger.warning(f"Facilitator rejected: {reason}")
            return {"verified": False, "reason": reason}

    except Exception as e:
        logger.error(f"Facilitator verify error: {e}")
        return {"verified": False, "reason": str(e)}


# ── Revenue Split ─────────────────────────────────────────────────────────────

async def split_payment(
    tool_developer_address: str,
    total_amount_usdc: str,
    gateway_fee_percent: float = 0.15
) -> dict:
    """
    Split a received payment: send tool developer's share to their wallet.
    Gateway keeps its cut automatically (it's already in the gateway wallet).
    
    Returns tx hash of the split payment.
    """
    server = get_server()
    gateway_keypair = Keypair.from_secret(settings.GATEWAY_SECRET_KEY)
    usdc = get_usdc_asset()
    
    total = Decimal(total_amount_usdc)
    developer_share = total * Decimal(str(1 - gateway_fee_percent))
    developer_share = developer_share.quantize(Decimal("0.0000001"))
    
    try:
        gateway_account = server.load_account(gateway_keypair.public_key)
        
        tx = (
            TransactionBuilder(
                source_account=gateway_account,
                network_passphrase=get_network_passphrase(),
                base_fee=100,
            )
            .append_payment_op(
                destination=tool_developer_address,
                asset=usdc,
                amount=str(developer_share),
            )
            .set_timeout(30)
            .build()
        )
        
        tx.sign(gateway_keypair)
        response = server.submit_transaction(tx)
        
        logger.info(f"Split sent {developer_share} USDC to {tool_developer_address}")
        return {
            "success": True,
            "developer_share": str(developer_share),
            "tx_hash": response.get("hash", ""),
        }
    
    except Exception as e:
        logger.error(f"Split payment error: {e}")
        return {"success": False, "reason": str(e)}


# ── Balance Check ─────────────────────────────────────────────────────────────

def get_usdc_balance(public_key: str) -> str:
    """Return USDC balance for a Stellar address."""
    server = get_server()
    try:
        account = server.load_account(public_key)
        for balance in account.raw_data.get("balances", []):
            if (
                balance.get("asset_code") == "USDC"
                and balance.get("asset_issuer") in [
                    settings.USDC_ISSUER_TESTNET,
                    settings.USDC_ISSUER_MAINNET,
                ]
            ):
                return balance.get("balance", "0")
        return "0"
    except Exception:
        return "0"
