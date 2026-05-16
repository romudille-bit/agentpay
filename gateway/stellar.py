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
      2. On *any* non-200 response (401, 5xx, network errors, timeouts) or
         on `isValid: false`, fall through to direct Horizon verification
         via `_verify_payment_horizon()`, which is the de facto production
         path. Agents must therefore hold a trivial XLM balance to cover
         the Stellar base fee on their own payment.

    Tier 2 #17 broadened the fallback from 401-only to all-non-200 + all
    exceptions. Previously, a 502 from the facilitator (or a network
    timeout) would skip the Horizon fallback and return failure, even
    though the payment may have been valid on-chain.

    Tier 2 #18 added the STELLAR_FACILITATOR_ENABLED flag (default False).
    When disabled, the OZ POST is skipped entirely and we go straight to
    Horizon — saves ~15s of wasted timeout per verification in the common
    case where OZ returns 401.

    Returns:
        {"verified": True, "tx_hash": "..."} on success
        {"verified": False, "reason": "..."} on failure
    """
    # Tier 2 #18: skip the OZ POST entirely when disabled. Saves a wasted
    # round-trip in production where OZ has been returning 401 since early
    # 2026. The Horizon fallback (now bulletproof after #17) runs identically
    # whether we got there via OZ-401 or via this short-circuit.
    if not settings.STELLAR_FACILITATOR_ENABLED:
        if tx_hash:
            return await _verify_payment_horizon(
                tx_hash, from_address, to_address, amount_usdc
            )
        return {
            "verified": False,
            "reason": "Facilitator disabled and no tx_hash provided",
        }

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
            # ANY non-200: fall back immediately to Horizon. Was 401-only;
            # broadened in #17 because the facilitator returns 5xx during
            # outages and other transient failures, all of which previously
            # produced spurious payment-verification failures even when the
            # tx was valid on-chain.
            if resp.status_code != 200:
                logger.warning(
                    f"OZ facilitator returned {resp.status_code} — falling back to Horizon verification"
                )
                if tx_hash:
                    return await _verify_payment_horizon(
                        tx_hash, from_address, to_address, amount_usdc
                    )
                return {
                    "verified": False,
                    "reason": f"Facilitator returned {resp.status_code} and no tx_hash provided",
                }
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
        # Connection errors, timeouts, malformed JSON, etc. — same fallback
        # as a non-200 status. Without this branch (added in #17), a
        # transient network blip on the facilitator host would fail the
        # payment even when on-chain settlement was successful.
        logger.warning(f"Facilitator unreachable ({e}) — falling back to Horizon verification")
        if tx_hash:
            return await _verify_payment_horizon(
                tx_hash, from_address, to_address, amount_usdc
            )
        return {"verified": False, "reason": f"Facilitator error: {e}"}


# ── Revenue Split ─────────────────────────────────────────────────────────────

async def split_payment(
    tool_developer_address: str,
    total_amount_usdc: str,
    gateway_fee_percent: float = 0.15,
    payment_id: str | None = None,
) -> dict:
    """
    Split a received payment: send tool developer's share to their wallet.
    Gateway keeps its cut automatically (it's already in the gateway wallet).

    Returns tx hash of the split payment.

    PR #14: if `payment_id` is provided, fire-and-forget a PATCH on
    `payment_logs` to mark the row as state='split_done' once the split
    tx settles. Intermediate state — eventually-consistent per the Q3
    decision in pr-14-plan.md. Not awaited because the caller (the
    asyncio.create_task in verify_and_fulfill) already isn't awaiting
    this whole function.
    """
    server = get_server()
    gateway_keypair = Keypair.from_secret(settings.GATEWAY_SECRET_KEY)
    usdc = get_usdc_asset()

    total = Decimal(total_amount_usdc)
    developer_share = total * Decimal(str(1 - gateway_fee_percent))
    developer_share = developer_share.quantize(Decimal("0.0000001"))

    try:
        # asyncio.to_thread keeps the event loop free while stellar_sdk's
        # synchronous Horizon call runs on a worker thread. Without this,
        # split_payment blocks the entire FastAPI worker for the 200-2000ms
        # of network round-trip — every concurrent call freezes during a split.
        gateway_account = await asyncio.to_thread(
            server.load_account, gateway_keypair.public_key
        )

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
        response = await asyncio.to_thread(server.submit_transaction, tx)
        split_tx_hash = response.get("hash", "")

        logger.info(f"Split sent {developer_share} USDC to {tool_developer_address}")

        # PR #14: PATCH payment_logs.state='split_done'. Lazy import to
        # avoid a circular import on module load (services.supabase
        # doesn't import stellar, but main.py imports both — direct
        # top-level import here would order-couple them).
        #
        # PR #14a fix: expected_state='verified' guards against the race
        # where this PATCH could land AFTER the route's awaited terminal
        # 'payment_done' PATCH and overwrite it. split_payment runs as
        # a fire-and-forget task scheduled from verify_and_fulfill — by
        # the time it completes (5-10s of Horizon round-trips), the
        # route has long since written 'payment_done'. The guard makes
        # the late split_done a silent no-op on the happy path; it
        # only lands if the row is still in 'verified' (which it never
        # is in current production, since the route writes payment_done
        # before split_payment finishes). This is acceptable —
        # split_done is informational, not load-bearing.
        if payment_id:
            try:
                from gateway.services.supabase import update_payment_log_state
                asyncio.create_task(update_payment_log_state(
                    payment_id, "split_done",
                    expected_state="verified",
                    gateway_fee_usdc=str(total - developer_share),
                ))
            except Exception as e:
                # Don't let analytics break the split — just log
                logger.warning(f"split_done PATCH failed to schedule: {e}")

        return {
            "success": True,
            "developer_share": str(developer_share),
            "tx_hash": split_tx_hash,
        }

    except Exception as e:
        logger.error(f"Split payment error: {e}")
        return {"success": False, "reason": str(e)}


# ── Refund ───────────────────────────────────────────────────────────────────

async def send_refund(
    agent_address: str,
    amount_usdc: str,
    payment_id: str,
) -> dict:
    """Send USDC from the gateway wallet back to the agent.

    PR #12: Option C from the Tier 2 design doc — pre-split rollback +
    async on-chain refund. Called by main.py:_refund_worker_loop when
    processing rows in state='refund_pending'.

    Structurally identical to split_payment but with:
      - destination = agent_address (not developer)
      - amount = full amount the agent paid (not the 85% dev share)
      - no gateway_fee_usdc PATCH on success (the fee is reversed)

    Returns:
      {"success": True,  "tx_hash": "...", "amount": "..."}  on success
      {"success": False, "reason": "..."}                    on failure

    Failure shapes we care about:
      'op_no_trust'                 — agent has no USDC trustline; manual
                                      reconciliation needed
      'tx_insufficient_fee'         — gateway low on XLM; retry later
      'op_underfunded'              — gateway low on USDC; retry later
      generic str(e)                — anything else, retry the row

    The gateway pays the Stellar network fee (~0.00001 XLM per refund).
    `_extract_stellar_reason`-style decoding of result_codes would
    give cleaner error strings — worth a follow-up; for now str(e)
    is enough for the retry loop to keep working.
    """
    if not settings.GATEWAY_SECRET_KEY:
        return {"success": False, "reason": "gateway_secret_not_configured"}

    server = get_server()
    gateway_keypair = Keypair.from_secret(settings.GATEWAY_SECRET_KEY)
    usdc = get_usdc_asset()
    amount = Decimal(amount_usdc).quantize(Decimal("0.0000001"))

    try:
        gateway_account = await asyncio.to_thread(
            server.load_account, gateway_keypair.public_key
        )

        tx = (
            TransactionBuilder(
                source_account=gateway_account,
                network_passphrase=get_network_passphrase(),
                base_fee=100,
            )
            .append_payment_op(
                destination=agent_address,
                asset=usdc,
                amount=str(amount),
            )
            # Add a memo so the on-chain audit trail ties the refund tx
            # back to the original challenge. payment_ids are 36-char
            # UUIDs; Stellar text memos are limited to 28 bytes. Slice
            # to fit — the prefix is enough to grep against payment_logs.
            .add_text_memo(f"refund:{payment_id[:20]}")
            .set_timeout(30)
            .build()
        )

        tx.sign(gateway_keypair)
        response = await asyncio.to_thread(server.submit_transaction, tx)
        refund_tx_hash = response.get("hash", "")

        logger.info(
            f"[REFUND] sent {amount} USDC to {agent_address[:8]}... "
            f"payment_id={payment_id[:8]}... tx={refund_tx_hash[:16]}..."
        )
        return {"success": True, "tx_hash": refund_tx_hash, "amount": str(amount)}

    except Exception as e:
        # Pull a clean reason out of the stellar-sdk exception. The full
        # str(e) often includes an XDR dump that's useless in logs; the
        # result_codes (e.g. 'op_no_trust') are the real signal. Lazy
        # import to avoid coupling at module-load time.
        try:
            from agentpay._wallet import _extract_stellar_reason
            reason = _extract_stellar_reason(e)
        except Exception:
            reason = str(e)[:200]
        logger.error(
            f"[REFUND] FAILED payment_id={payment_id[:8]}... reason={reason}"
        )
        return {"success": False, "reason": reason}


# ── Balance Check ─────────────────────────────────────────────────────────────

async def get_usdc_balance(public_key: str) -> str:
    """Return USDC balance for a Stellar address.

    Async because stellar_sdk's Server.load_account is synchronous and would
    otherwise block the event loop for 200-2000ms per call. Wrapping in
    asyncio.to_thread offloads to a worker thread. Callers must `await`.
    """
    server = get_server()
    try:
        account = await asyncio.to_thread(server.load_account, public_key)
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
