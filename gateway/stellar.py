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
from gateway.config import settings
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

async def _verify_payment_horizon(
    tx_hash: str,
    from_address: str,
    to_address: str,
    amount_usdc: str,
) -> dict:
    """
    Direct Horizon verification — the actual production path on both mainnet
    and testnet. The OZ facilitator has returned 401 since early 2026, so
    verify_payment() always falls through to this function.

    Queries Horizon for the transaction and checks:
      - transaction exists and was successful
      - contains a USDC payment from `from_address` to `to_address`
      - asset code is USDC with the correct issuer for this network
      - amount paid >= amount_usdc required
    """
    horizon_url = (
        "https://horizon-testnet.stellar.org"
        if settings.STELLAR_NETWORK == "testnet"
        else "https://horizon.stellar.org"
    )
    usdc_issuer = (
        settings.USDC_ISSUER_TESTNET
        if settings.STELLAR_NETWORK == "testnet"
        else settings.USDC_ISSUER_MAINNET
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Fetch transaction
            resp = await client.get(f"{horizon_url}/transactions/{tx_hash}")
            if resp.status_code == 404:
                return {"verified": False, "reason": "Transaction not found on Horizon"}
            if resp.status_code != 200:
                return {"verified": False, "reason": f"Horizon returned {resp.status_code}"}
            tx_data = resp.json()
            if not tx_data.get("successful", False):
                return {"verified": False, "reason": "Transaction was not successful"}

            # Fetch operations for this transaction
            ops_resp = await client.get(f"{horizon_url}/transactions/{tx_hash}/operations")
            if ops_resp.status_code != 200:
                return {"verified": False, "reason": "Could not fetch transaction operations"}
            ops = ops_resp.json().get("_embedded", {}).get("records", [])

            required = Decimal(amount_usdc)
            for op in ops:
                if op.get("type") != "payment":
                    continue
                if op.get("asset_code") != "USDC":
                    continue
                if op.get("asset_issuer") != usdc_issuer:
                    continue
                if op.get("to") != to_address:
                    continue
                if op.get("from") != from_address:
                    continue
                paid = Decimal(op.get("amount", "0"))
                if paid < required:
                    return {"verified": False, "reason": f"Paid {paid} USDC but {required} required"}
                logger.info(f"Payment verified via Horizon (testnet fallback): {tx_hash}")
                return {"verified": True, "tx_hash": tx_hash}

            return {"verified": False, "reason": "No matching USDC payment found in transaction"}

    except Exception as e:
        logger.error(f"Horizon direct verify error: {e}")
        return {"verified": False, "reason": str(e)}


async def verify_payment(
    from_address: str,
    to_address: str,
    amount_usdc: str,
    payment_id: str,
    max_age_seconds: int = 60,
    tx_hash: str = "",
) -> dict:
    """
    Verify a USDC payment on Stellar.

    Flow:
      1. Attempt the OpenZeppelin x402 facilitator (`STELLAR_FACILITATOR_URL`).
         Historically this would also sponsor XLM network fees, but as of
         early 2026 the facilitator returns 401 on both mainnet and testnet
         for all requests — the branch is kept so we pick up free sponsorship
         again if/when auth is relaxed or we wire up credentials.
      2. On 401 (or any rejection), fall through to direct Horizon
         verification via `_verify_payment_horizon()`, which is the de facto
         production path. Agents must therefore hold a trivial XLM balance
         to cover the Stellar base fee on their own payment.

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
            # Step 1: verify the payment exists and is valid
            resp = await client.post(
                f"{facilitator_url}/verify",
                json=payload
            )
            # 401 = facilitator requires auth — fall back immediately
            if resp.status_code == 401:
                logger.warning("OZ facilitator returned 401 — falling back to Horizon verification")
                if tx_hash:
                    return await _verify_payment_horizon(
                        tx_hash, from_address, to_address, amount_usdc
                    )
                return {"verified": False, "reason": "Facilitator unavailable and no tx_hash provided"}
            data = resp.json()
            if not data.get("isValid"):
                reason = data.get("invalidReason", "Facilitator rejected payment")
                logger.warning(f"Facilitator rejected: {reason}")
                # OZ facilitator now requires auth on both mainnet and testnet (returns 401).
                # Fall back to direct Horizon verification for all networks.
                if tx_hash:
                    logger.info(f"Falling back to direct Horizon verification for {tx_hash[:16]}...")
                    return await _verify_payment_horizon(
                        tx_hash, from_address, to_address, amount_usdc
                    )
                return {"verified": False, "reason": reason}

            tx_hash = data.get("txHash", "")
            logger.info(f"Payment verified via OZ facilitator: {tx_hash}")

            # Step 2: settle — marks the payment as fulfilled on the facilitator's side.
            # This closes the channel and prevents replay even if our in-memory
            # _completed_payments set is cleared on redeploy.
            try:
                settle_resp = await client.post(
                    f"{facilitator_url}/settle",
                    json=payload
                )
                settle_data = settle_resp.json()
                if not settle_data.get("success", True):  # treat missing key as ok
                    logger.warning(f"Facilitator settle returned non-success: {settle_data}")
                else:
                    logger.info(f"Payment settled via OZ facilitator: {tx_hash}")
            except Exception as settle_err:
                # Non-fatal — payment is verified and USDC is on-chain.
                # Log the error but do not block data delivery.
                logger.error(f"Facilitator settle error (non-fatal): {settle_err}")

            return {"verified": True, "tx_hash": tx_hash}

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
