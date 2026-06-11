"""
radar_settle.py — verify a RadarSplit on-chain settlement (Arbitrum stack).

Day-4 gateway adapter for the Arbitrum x402 Radar. After an agent pays a
Radar-listed project through the `RadarSplit` contract, the gateway confirms the
payment by reading the `Settled` event from the tx receipt over JSON-RPC — the
same pattern as `gateway/base.py:verify_base_tx`, adapted to RadarSplit's event.

SECURITY (per code review): the replay guard in the contract is namespaced by
payer, so the gateway MUST verify ALL fields, not just paymentId:
  - the log was emitted by the CANONICAL RadarSplit contract address,
  - paymentId, payer, developer match what the gateway issued,
  - devAmount + fee >= the required amount.
Matching paymentId alone is insufficient — anyone can emit a same-id event from a
different contract or settle a different (paymentId, payer) pair.

The parsing (`parse_settled_log`) is a pure function so it's unit-testable against
a crafted receipt; `verify_radar_settlement` adds the JSON-RPC I/O.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# keccak256("Settled(bytes32,address,address,uint256,uint256,address)")
# (computed with `cast keccak` — see tests/test_radar_settle.py for the guard).
SETTLED_TOPIC0 = "0x1ce106df608796d45c75a8d65185553b983f705bba07d2287116aa65bb156f1b"


def _addr_topic(addr: str) -> str:
    """Left-pad a 20-byte address to a 32-byte topic, lowercased."""
    return "0x" + addr.lower().removeprefix("0x").zfill(64)


def _norm_bytes32(b32: str) -> str:
    """Normalize a bytes32 hex string (paymentId) to lowercase 0x + 64 hex."""
    return "0x" + b32.lower().removeprefix("0x").zfill(64)


def parse_settled_log(
    receipt: dict,
    contract: str,
    payment_id: str,
    payer: str,
    developer: str,
    required_amount_atomic: int,
    fee_recipient: Optional[str] = None,
) -> dict:
    """Pure: scan a tx receipt for a matching RadarSplit `Settled` event.

    Returns {"success": bool, "reason": str, "dev_amount": int, "fee": int}.
    All identity fields must match; the paid total (devAmount + fee) must be
    >= required. `fee_recipient`, if given, is also checked.
    """
    if not isinstance(receipt, dict):
        return {"success": False, "reason": "no_receipt", "dev_amount": 0, "fee": 0}
    if receipt.get("status") != "0x1":
        return {"success": False, "reason": "tx_reverted", "dev_amount": 0, "fee": 0}

    want_contract = contract.lower()
    want_pid = _norm_bytes32(payment_id)
    want_payer = _addr_topic(payer)
    want_dev = _addr_topic(developer)

    for log in receipt.get("logs", []):
        # Must come from the canonical RadarSplit contract — not just any log.
        if (log.get("address") or "").lower() != want_contract:
            continue
        topics = log.get("topics", [])
        if len(topics) < 4 or topics[0].lower() != SETTLED_TOPIC0:
            continue
        if topics[1].lower() != want_pid:
            continue
        if topics[2].lower() != want_payer:
            continue
        if topics[3].lower() != want_dev:
            continue

        # Non-indexed data: devAmount (uint256), fee (uint256), feeRecipient (address).
        raw = (log.get("data") or "0x").removeprefix("0x")
        if len(raw) < 192:  # 3 * 32 bytes
            return {"success": False, "reason": "malformed_event_data", "dev_amount": 0, "fee": 0}
        dev_amount = int(raw[0:64], 16)
        fee = int(raw[64:128], 16)
        emitted_fee_recipient = "0x" + raw[128:192][24:]  # last 20 bytes of the 32-byte word

        if fee_recipient is not None and emitted_fee_recipient.lower() != fee_recipient.lower():
            return {"success": False, "reason": "fee_recipient_mismatch", "dev_amount": dev_amount, "fee": fee}

        total = dev_amount + fee
        if total < required_amount_atomic:
            return {
                "success": False,
                "reason": f"insufficient_settlement: got {total}, need {required_amount_atomic}",
                "dev_amount": dev_amount, "fee": fee,
            }
        return {"success": True, "reason": "ok", "dev_amount": dev_amount, "fee": fee}

    return {"success": False, "reason": "no_matching_settled_event", "dev_amount": 0, "fee": 0}


async def verify_radar_settlement(
    tx_hash: str,
    contract: str,
    payment_id: str,
    payer: str,
    developer: str,
    required_amount_atomic: int,
    rpc_url: str,
    fee_recipient: Optional[str] = None,
) -> dict:
    """Fetch the tx receipt via JSON-RPC and verify the RadarSplit `Settled` event.

    Returns {"success", "tx_hash", "reason", "dev_amount", "fee"}.
    """
    base = {"tx_hash": tx_hash, "dev_amount": 0, "fee": 0}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "eth_getTransactionReceipt", "params": [tx_hash]},
                headers={"Content-Type": "application/json"},
            )
    except Exception as e:
        logger.warning("Radar settle: RPC unreachable: %s", e)
        return {**base, "success": False, "reason": "rpc_unreachable"}

    if resp.status_code != 200:
        return {**base, "success": False, "reason": f"rpc_http_{resp.status_code}"}

    receipt = resp.json().get("result")
    if receipt is None:
        return {**base, "success": False, "reason": "tx_not_found_or_pending"}

    out = parse_settled_log(
        receipt, contract=contract, payment_id=payment_id, payer=payer,
        developer=developer, required_amount_atomic=required_amount_atomic,
        fee_recipient=fee_recipient,
    )
    if out["success"]:
        logger.info("[RADAR] settlement verified: tx=%s... dev_amount=%s fee=%s",
                    tx_hash[:20], out["dev_amount"], out["fee"])
    return {**out, "tx_hash": tx_hash}
