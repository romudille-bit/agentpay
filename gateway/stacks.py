"""
gateway/stacks.py — Stacks/sBTC settlement adapter (AGE-23).

STATUS: SKELETON (2026-07-20). Not imported by main.py/routes yet — safe to
ship inert. Design doc: docs/stacks-adapter.md (12-point checklist).

Third settlement adapter, third settlement model:
  - Stellar (stellar.py): agent broadcasts; gateway only verifies.
  - Base (base.py): client signs an off-chain EIP-3009 auth; CDP broadcasts.
  - Stacks (here): client hands us a FULLY SIGNED, UNBROADCAST tx and the
    GATEWAY broadcasts — via the facilitator's /settle, or directly to Hiro
    when the facilitator is down. Every failure mode between broadcast and
    confirmation lands on us.

Hard requirements (each anchored where it must be enforced):

  [CHECKLIST #6] — replay consume FAILS CLOSED and happens PRE-settle.
      Stacks txid is deterministic from the signed tx before broadcast
      (agentpay._stacks_tx.txid_of), so consume the txid BEFORE /settle using
      the same in-memory check-and-add + AWAITED Supabase insert pattern as
      base.py:settle_base_payment (TOCTOU rationale: docs/DESIGN_NOTES.md).

  [CHECKLIST #5] — verification decodes the Clarity contract call
      (sbtc-token::transfer args: amount, sender, recipient, memo) and binds
      memo → payment_id. Amount alone is meaningless when every tool costs
      the same (AGE-64 lesson).

  [CHECKLIST #10] — the receipt insert is AWAITED before the terminal
      payment_done PATCH (H5 receipt-write race).

  Recovery from day one (`settle_exact_node_failure` → `ok_recovered`,
      live Base incident 2026-06-11): on settle timeout/error, poll Hiro by
      txid; if the tx confirmed, fulfil as `ok_recovered` instead of charging
      the agent for nothing.

  Facilitator posture (documented in docs/stacks-adapter.md "known limitations"): facilitator is
      convenience, not a hard dependency. Two young stacks exist (tony1908's
      x402-stacks; aibtcdev worker + sponsor-relay). The payment artifact is
      a complete signed tx → a dead facilitator degrades to direct
      `POST /v2/transactions` on Hiro + confirmation polling.

  Header dialect #3: lowercase `payment-required` / `payment-signature` /
      `payment-response`, CAIP-2 `stacks:1` / `stacks:2147483648`. Parse
      case-insensitively; never mix with X-Payment (Stellar) or
      PAYMENT-SIGNATURE (Base) handling.

Proposed settings (config.py additions when wiring starts):
    STACKS_NETWORK            ("testnet" | "mainnet")
    STACKS_HIRO_API           (https://api.testnet.hiro.so / api.hiro.so)
    STACKS_FACILITATOR_URL    (empty ⇒ direct-broadcast mode only)
    STACKS_SBTC_CONTRACT      (defaults per network from _stacks_tx)
    STACKS_GATEWAY_ADDRESS    (c32; receives payments — fund STX for fees)
    STACKS_SETTLE_TIMEOUT_S   (default 30)
    STACKS_CONFIRM_POLL_S / STACKS_CONFIRM_MAX_POLLS

Mirrors the 4-stage structure of gateway/routes/tools.py:
    402-issue → verify → settle/broadcast → fulfil+record.

Acceptance (AGE-23): testnet payment settles through the facilitator;
kill-the-facilitator test settles via direct Hiro broadcast; replayed txid
rejected; settle-timeout path produces ok_recovered.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("gateway.stacks")

__all__ = [
    "verify_stacks_payment",
    "settle_stacks_payment",
    "poll_confirmation",
]


async def verify_stacks_payment(
    payment_header: str,
    *,
    expected_amount_sats: int,
    expected_recipient: str,
    payment_id: str,
) -> dict:
    """Decode + statically verify a signed-but-unbroadcast sBTC transfer.

    Steps (NO network I/O except a nonce/balance sanity read):
      1. base64-decode the lowercase `payment-signature` payload.
      2. Deserialize (SIP-005); require a contract call on the configured
         sbtc-token contract, function `transfer`.
      3. [CHECKLIST #5] decode Clarity args; require memo == payment_id
         (prefix rule as Stellar: startswith either way), recipient ==
         expected_recipient, amount >= expected_amount_sats (small overpay
         tolerance only — AGE-24 owns the FX/tolerance numbers).
      4. Require the post-condition asserting exactly `amount` sats leave the
         sender — refuse unsafe txs rather than broadcasting them.
      5. Compute txid = txid_of(signed_tx) for the caller's replay consume.

    Returns {"authorized": bool, "reason": str, "txid": str, "sender": str,
             "amount_sats": int} — same contract shape as stellar/base verify.
    """
    raise NotImplementedError("AGE-23")


async def settle_stacks_payment(
    signed_tx: bytes,
    txid: str,
    *,
    payment_id: str,
) -> dict:
    """Broadcast + confirm. THE ORDER IS THE SECURITY MODEL:

      1. [CHECKLIST #6] atomically consume `txid` (in-memory check-and-add +
         AWAITED Supabase insert, fail-CLOSED on infra error) — BEFORE any
         broadcast attempt. A replayed txid dies here.
      2. Try facilitator /settle when STACKS_FACILITATOR_URL is set.
      3. Facilitator down/timeout/5xx → direct `POST /v2/transactions` (Hiro).
      4. Timeout or ambiguous error AFTER broadcast → poll_confirmation();
         confirmed ⇒ return state "ok_recovered" (never charge-for-nothing,
         never double-broadcast: same txid ⇒ node-level idempotent).
      5. Definitive rejection (node rejects tx) ⇒ un-consume is NOT allowed —
         the consume stays (fail-closed); return rejected with the node
         reason.

    Returns {"ok": bool, "state": "ok"|"ok_recovered"|"rejected"|"uncertain",
             "txid": str, "reason": str}.
    Caller (routes) must [CHECKLIST #10] await the receipt insert before the
    terminal payment_done PATCH, and use expected_state guards on every
    header-keyed PATCH (F3 lesson).
    """
    raise NotImplementedError("AGE-23")


async def poll_confirmation(txid: str, *, max_polls: Optional[int] = None) -> dict:
    """GET /extended/v1/tx/{txid} until success/abort/timeout.

    Used by the ok_recovered path and by the fulfil loop. Distinguish:
    tx_status success / abort_by_response / abort_by_post_condition (the
    post-condition doing its job — report as rejected, not uncertain) /
    not-found-yet (keep polling).
    """
    raise NotImplementedError("AGE-23")
