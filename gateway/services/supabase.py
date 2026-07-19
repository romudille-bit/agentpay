"""
services/supabase.py — Supabase REST helpers.

Wraps the Supabase REST API in raw httpx — works with the sb_secret_ key
format that the supabase-py SDK can't handle.

This grew from the original log_payment helper into the
persisted replay-state home. Functions are grouped:

    Replay protection
        record_payment_id, is_payment_id_consumed
        record_tx_hash,    is_tx_hash_consumed

    Pending challenges (#13 Group 2 — pending)
    Faucet IP cooldown (#13 Group 3 — pending)
    payment_logs lifecycle (#13 Group 4 — pending)

Dual-write phase: writes go to Supabase as a secondary store, reads
still come from in-memory dicts. Cutover (Supabase becomes primary)
is row 7 of the Tier 2 plan.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from gateway.config import settings

logger = logging.getLogger(__name__)


# Standard timeouts. Reads are short (we want to fail fast and let in-memory
# take over); writes are slightly longer (they're fire-and-forget anyway, but
# we want to give Supabase a fair shot).
_READ_TIMEOUT  = 3.0
_WRITE_TIMEOUT = 5.0


def sb_headers() -> dict:
    """Headers for Supabase REST API calls."""
    return {
        "apikey":        settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


def sb_enabled() -> bool:
    return bool(settings.SUPABASE_URL and settings.SUPABASE_KEY)


# log_payment (the legacy "single INSERT at end of call_tool") was removed
# The current pattern is:
#   1. insert_pending_payment_log() at 402-issue time → state='pending' row
#   2. update_payment_log_state() at each lifecycle transition (verified,
#      split_done, payment_done, rejected, abandoned, refund_pending)
# See routes/tools.py:call_tool for the integration site, and §5 of the
# Tier 2 design doc for the state machine.


# ─────────────────────────────────────────────────────────────────────────────
# Replay protection
# ─────────────────────────────────────────────────────────────────────────────
#
# Two tables, both insert-only:
#   replay_payment_ids — UUID side. PK is payment_id alone.
#   replay_tx_hashes   — hash side. Composite PK on (tx_hash, network) so
#                        a Stellar testnet hash can't collide with a Base
#                        mainnet hash.
#
# Behaviour conventions for this group (AGE-60 — fail CLOSED):
#   record_*()  — tri-state:
#                   True  — newly recorded (payment may proceed)
#                   False — row already exists (HTTP 409 → replay, reject)
#                   None  — infra error (network, 5xx, broken table/RLS).
#                 Supabase is now the PRIMARY replay store — the in-memory
#                 sets are wiped on every Railway restart, so "don't block
#                 on infrastructure errors" made a pre-restart payment
#                 replayable during any Supabase blip. Callers MUST treat
#                 None as "consume not confirmed" and reject WITHOUT
#                 accusing replay (the client may retry the same proof).
#   is_*_consumed() — pre-checks only: True if row exists, False otherwise
#                 (including on error, logged). They are advisory — the
#                 authoritative, fail-closed gate is the record_*() insert
#                 (PK/composite-PK 409), which every consume path awaits.


# AGE-60: sustained-failure escalation. A single blip is a warning; a broken
# table / RLS misconfiguration is silent replay-protection loss and must be
# LOUD. Counter is shared by both record_* helpers and resets on any success.
_replay_store_consecutive_failures = 0
_REPLAY_STORE_ALERT_THRESHOLD = 3


def _replay_store_failed(what: str, detail: str) -> None:
    global _replay_store_consecutive_failures
    _replay_store_consecutive_failures += 1
    n = _replay_store_consecutive_failures
    logger.error(f"{what} Supabase failure ({detail}) — consume NOT confirmed")
    if n >= _REPLAY_STORE_ALERT_THRESHOLD:
        logger.critical(
            f"[ALERT] durable replay store failing ({n} consecutive failures) — "
            f"paid consumes are being rejected fail-closed; check Supabase "
            f"availability / replay_* table RLS"
        )


def _replay_store_ok() -> None:
    global _replay_store_consecutive_failures
    _replay_store_consecutive_failures = 0


async def record_payment_id(payment_id: str) -> bool | None:
    """Insert payment_id into replay_payment_ids.

    Returns:
        True  — newly recorded
        False — already consumed (HTTP 409 conflict) → replay, reject
        None  — infra error: consume NOT confirmed (AGE-60 fail-closed).
                Callers must reject without fulfilling, with a retryable
                reason (not a replay accusation).
        (sb disabled → True: single-process in-memory dedupe is authoritative)
    """
    if not sb_enabled():
        return True
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/replay_payment_ids",
                headers=sb_headers(),
                json={"payment_id": payment_id},
            )
        if resp.status_code == 409:
            _replay_store_ok()
            return False  # already consumed
        if resp.status_code not in (200, 201):
            _replay_store_failed(
                "record_payment_id",
                f"HTTP {resp.status_code} body={resp.text[:200]} payment_id={payment_id}",
            )
            return None
        _replay_store_ok()
        return True
    except Exception as e:
        _replay_store_failed("record_payment_id", f"payment_id={payment_id}: {e}")
        return None


async def unrecord_tx_hash(tx_hash: str, network: str) -> bool:
    """Best-effort compensating DELETE for a HALF-consumed proof (AGE-60
    follow-up): record_tx_hash landed but the paired record_payment_id could
    not be confirmed, so verify_and_fulfill is rolling the consume back to
    keep the client's proof retryable.

    Returns True when the row is gone. On failure logs CRITICAL with the
    identifiers — until the row is removed, this proof will false-positive
    as "replay attack" on retry and needs manual reconciliation.
    """
    if not sb_enabled():
        return True
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.SUPABASE_URL}/rest/v1/replay_tx_hashes",
                headers=sb_headers(),
                params={"tx_hash": f"eq.{tx_hash}", "network": f"eq.{network}"},
            )
        if resp.status_code in (200, 204):
            return True
        logger.critical(
            f"[ALERT] unrecord_tx_hash failed: HTTP {resp.status_code} "
            f"tx_hash={tx_hash[:16]}... network={network} — half-consumed proof "
            f"will reject as replay until this row is deleted manually"
        )
        return False
    except Exception as e:
        logger.critical(
            f"[ALERT] unrecord_tx_hash failed: {e} tx_hash={tx_hash[:16]}... "
            f"network={network} — half-consumed proof will reject as replay "
            f"until this row is deleted manually"
        )
        return False


async def is_payment_id_consumed(payment_id: str) -> bool:
    """Returns True if payment_id is already in replay_payment_ids.

    Used during cutover (#13 row 7) — not called in this PR.
    """
    if not sb_enabled():
        return False
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/replay_payment_ids",
                headers=sb_headers(),
                params={"payment_id": f"eq.{payment_id}", "select": "payment_id"},
            )
        if resp.status_code != 200:
            logger.error(
                f"is_payment_id_consumed Supabase error: HTTP {resp.status_code}"
            )
            return False
        return len(resp.json()) > 0
    except Exception as e:
        logger.error(f"is_payment_id_consumed failure (payment_id={payment_id}): {e}")
        return False


async def record_tx_hash(tx_hash: str, network: str) -> bool | None:
    """Insert (tx_hash, network) into replay_tx_hashes.

    network must be one of: 'stellar-mainnet', 'stellar-testnet',
    'base-mainnet', 'base-sepolia'. Composite PK means the same hash can
    exist across networks (extremely unlikely, but defensive).

    Returns:
        True  — newly recorded
        False — already consumed (HTTP 409 conflict) → replay, reject
        None  — infra error: consume NOT confirmed (AGE-60 fail-closed).
                Callers must reject without fulfilling, with a retryable
                reason (not a replay accusation).
        (sb disabled → True: single-process in-memory dedupe is authoritative)
    """
    if not sb_enabled():
        return True
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/replay_tx_hashes",
                headers=sb_headers(),
                json={"tx_hash": tx_hash, "network": network},
            )
        if resp.status_code == 409:
            _replay_store_ok()
            return False
        if resp.status_code not in (200, 201):
            _replay_store_failed(
                "record_tx_hash",
                f"HTTP {resp.status_code} body={resp.text[:200]} "
                f"tx_hash={tx_hash[:16]}... network={network}",
            )
            return None
        _replay_store_ok()
        return True
    except Exception as e:
        _replay_store_failed(
            "record_tx_hash", f"tx_hash={tx_hash[:16]}..., network={network}: {e}"
        )
        return None


async def is_tx_hash_consumed(tx_hash: str, network: str) -> bool:
    """Returns True if (tx_hash, network) is already in replay_tx_hashes.

    Used during cutover (#13 row 7) — not called in this PR.
    """
    if not sb_enabled():
        return False
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/replay_tx_hashes",
                headers=sb_headers(),
                params={
                    "tx_hash": f"eq.{tx_hash}",
                    "network": f"eq.{network}",
                    "select": "tx_hash",
                },
            )
        if resp.status_code != 200:
            logger.error(
                f"is_tx_hash_consumed Supabase error: HTTP {resp.status_code}"
            )
            return False
        return len(resp.json()) > 0
    except Exception as e:
        logger.error(
            f"is_tx_hash_consumed failure "
            f"(tx_hash={tx_hash[:16]}..., network={network}): {e}"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Pending challenges
# ─────────────────────────────────────────────────────────────────────────────
#
# Mirrors the in-memory _pending_challenges dict in gateway/x402.py. The
# dataclass PaymentChallenge stores expires_at as a Unix float; the table
# stores it as timestamptz. _to_iso() handles the conversion.
#
# Behaviour conventions:
#   store_pending_challenge() — INSERT new row. On error, log + swallow
#       (in-memory dict is still primary in this PR).
#   get_pending_challenge() — SELECT WHERE expires_at > now(). Returns
#       None if not found, expired, or on error. Used during cutover.
#   delete_pending_challenge() — DELETE by payment_id. Idempotent (no-op
#       on missing row).
#   cleanup_expired_challenges() — DELETE rows where expires_at < now() -
#       interval '1 hour'. Returns count of deleted rows. Per yesterday's
#       decision, just exposed; scheduling lands in #13 cutover (row 7).


def _unix_to_iso(unix_ts: float) -> str:
    """Convert Unix float timestamp → ISO 8601 with timezone for Postgres."""
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()


async def store_pending_challenge(
    payment_id: str,
    tool_name: str,
    amount_usdc: str,
    gateway_address: str,
    developer_address: str,
    expires_at: float,
    request_data: dict,
) -> None:
    """INSERT into pending_challenges. Fire-and-forget."""
    if not sb_enabled():
        return
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/pending_challenges",
                headers=sb_headers(),
                json={
                    "payment_id":        payment_id,
                    "tool_name":         tool_name,
                    "amount_usdc":       amount_usdc,
                    "gateway_address":   gateway_address,
                    # Pass NULL (not empty string) so the column is genuinely
                    # null in the DB for AgentPay-owned tools.
                    "developer_address": developer_address or None,
                    "request_data":      request_data,
                    "expires_at":        _unix_to_iso(expires_at),
                },
            )
        if resp.status_code not in (200, 201):
            logger.error(
                f"store_pending_challenge Supabase error: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (payment_id={payment_id})"
            )
    except Exception as e:
        logger.error(
            f"store_pending_challenge failure (payment_id={payment_id}): {e}"
        )


async def get_pending_challenge(payment_id: str) -> Optional[dict]:
    """SELECT a non-expired challenge by payment_id.

    Returns the row as a dict, or None if not found / expired / on error.
    Used during cutover (#13 row 7) — not called in this PR.
    """
    if not sb_enabled():
        return None
    try:
        # ISO 8601 of "now" for the expires_at filter
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/pending_challenges",
                headers=sb_headers(),
                params={
                    "payment_id":  f"eq.{payment_id}",
                    "expires_at":  f"gt.{now_iso}",
                    "select":      "*",
                },
            )
        if resp.status_code != 200:
            logger.error(
                f"get_pending_challenge Supabase error: HTTP {resp.status_code}"
            )
            return None
        rows = resp.json()
        return rows[0] if rows else None
    except Exception as e:
        logger.error(f"get_pending_challenge failure (payment_id={payment_id}): {e}")
        return None


async def delete_pending_challenge(payment_id: str) -> None:
    """DELETE a challenge row by payment_id. Idempotent."""
    if not sb_enabled():
        return
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.SUPABASE_URL}/rest/v1/pending_challenges",
                headers=sb_headers(),
                params={"payment_id": f"eq.{payment_id}"},
            )
        if resp.status_code not in (200, 204):
            logger.error(
                f"delete_pending_challenge error: HTTP {resp.status_code}"
            )
    except Exception as e:
        logger.error(
            f"delete_pending_challenge failure (payment_id={payment_id}): {e}"
        )


async def cleanup_expired_challenges() -> int:
    """DELETE rows where expires_at < now() - interval '1 hour'.

    Returns the number of rows deleted (or 0 on error / Supabase disabled).
    Just exposed in this PR — scheduling comes with #13 cutover (row 7).
    """
    if not sb_enabled():
        return 0
    try:
        # 1 hour ago in ISO 8601
        from datetime import timedelta
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.SUPABASE_URL}/rest/v1/pending_challenges",
                # Prefer: return=representation makes Supabase echo the
                # deleted rows so we can count them.
                headers={**sb_headers(), "Prefer": "return=representation"},
                params={"expires_at": f"lt.{cutoff}"},
            )
        if resp.status_code not in (200, 204):
            logger.error(
                f"cleanup_expired_challenges error: HTTP {resp.status_code}"
            )
            return 0
        # 204 returns no body; 200 with return=representation returns the
        # deleted rows. Count whatever we got.
        if resp.status_code == 200:
            return len(resp.json())
        return 0
    except Exception as e:
        logger.error(f"cleanup_expired_challenges failure: {e}")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Faucet IP cooldown
# ─────────────────────────────────────────────────────────────────────────────
#
# Replaces the in-memory _FAUCET_IP_LOG dict in routes/faucet.py. Two
# functions:
#
#   faucet_ip_seen_recently() — read with a cooldown filter. Returns True
#       if the IP requested a faucet wallet within the cooldown window.
#       Used during cutover (#13 row 7) to enforce per-IP rate limit.
#   record_faucet_ip() — UPSERT. Either inserts a new row or updates
#       last_used to now() if the IP exists. Uses Postgres ON CONFLICT
#       via the Supabase upsert preference.
#
# The faucet only runs on testnet, so this table only ever sees testnet
# traffic. The IP column type is `inet` — Supabase accepts plain strings.


async def faucet_ip_seen_recently(ip: str, cooldown_seconds: int) -> bool:
    """Returns True if `ip` requested a faucet wallet within the last
    `cooldown_seconds`. False if not, or on Supabase error.
    """
    if not sb_enabled():
        return False
    try:
        from datetime import timedelta
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=cooldown_seconds)
        ).isoformat()
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/faucet_ip_log",
                headers=sb_headers(),
                params={
                    "ip":         f"eq.{ip}",
                    "last_used":  f"gt.{cutoff}",
                    "select":     "ip",
                },
            )
        if resp.status_code != 200:
            logger.error(
                f"faucet_ip_seen_recently Supabase error: HTTP {resp.status_code}"
            )
            return False
        return len(resp.json()) > 0
    except Exception as e:
        logger.error(f"faucet_ip_seen_recently failure (ip={ip}): {e}")
        return False


async def record_faucet_ip(ip: str) -> None:
    """UPSERT — insert (ip, now()) or update last_used = now() if row exists.

    Supabase REST upsert: POST with `Prefer: resolution=merge-duplicates`
    handles the ON CONFLICT path natively.
    """
    if not sb_enabled():
        return
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/faucet_ip_log",
                # merge-duplicates triggers Postgres ON CONFLICT (ip) DO UPDATE
                headers={
                    **sb_headers(),
                    "Prefer": "return=minimal,resolution=merge-duplicates",
                },
                json={
                    "ip":        ip,
                    "last_used": datetime.now(tz=timezone.utc).isoformat(),
                },
            )
        if resp.status_code not in (200, 201, 204):
            logger.error(
                f"record_faucet_ip Supabase error: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (ip={ip})"
            )
    except Exception as e:
        logger.error(f"record_faucet_ip failure (ip={ip}): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# payment_logs lifecycle
# ─────────────────────────────────────────────────────────────────────────────
#
# Foundation for #14 (payment_logs lifecycle state machine). This PR exposes
# the insert + update primitives; #14 wires them into the route handler.
#
#   insert_pending_payment_log() — INSERT row with state='pending'. Returns
#       the newly inserted id (used by callers to locate the row for
#       subsequent state updates).
#   update_payment_log_state() — UPDATE state + arbitrary fields by
#       payment_id. The set_updated_at_payment_logs trigger handles
#       updated_at automatically.
#
# State machine (per design doc §5.3):
#   pending → verified → split_done → payment_done       (happy path)
#   pending → abandoned                                   (TTL expired)
#   pending → rejected                                    (replay/forged)
#   verified → refund_pending → refund_done|refund_failed (#12 territory)


async def insert_pending_payment_log(
    payment_id: str,
    tool_name: str,
    network: str,
    amount_usdc: str,
    *,
    state: str = "pending",
    agent_address: Optional[str] = None,
    tx_hash: Optional[str] = None,
    developer_address: Optional[str] = None,
    gateway_fee_usdc: Optional[str] = None,
    client_ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Optional[int]:
    """INSERT a new payment_logs row.

    Required: payment_id, tool_name, network, amount_usdc.
    All other fields are optional at insert time.

    `state` defaults to 'pending' — the normal pre-402 case for Stellar
    where agent_address and tx_hash aren't known yet. Also used
    state='payment_done' to insert a complete row in one round trip for
    the Base success path, where the original UUID-keyed pending row is
    stranded (x402-v2 doesn't carry payment_id back through
    PAYMENT-SIGNATURE) and we have to write a second row keyed on
    tx_hash anyway.

    Returns the newly-inserted id (for the caller to remember and use in
    subsequent updates), or None on error / Supabase disabled. Error
    path doesn't raise so callers can decide whether to fail-closed
    or continue — routes/tools.py's pre-402 hook fails closed with 503.
    """
    if not sb_enabled():
        return None
    payload = {
        "payment_id":   payment_id,
        "tool_name":    tool_name,
        "network":      network,
        "amount_usdc":  amount_usdc,
        "state":        state,
        # Legacy `status` column kept populated for backward compat.
        # Mirrors the state machine values so analytics queries on the
        # old column still surface useful data.
        "status":       state,
    }
    # Only include optional fields if non-None. Avoids overwriting Supabase
    # column defaults with explicit nulls.
    for key, val in {
        "agent_address":     agent_address,
        "tx_hash":           tx_hash,
        "developer_address": developer_address,
        "gateway_fee_usdc":  gateway_fee_usdc,
        "client_ip":         client_ip,
        "user_agent":        user_agent,
    }.items():
        if val is not None:
            payload[key] = val

    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                # return=representation gives us back the inserted row so
                # we can grab the auto-generated id.
                headers={**sb_headers(), "Prefer": "return=representation"},
                json=payload,
            )
        if resp.status_code not in (200, 201):
            logger.error(
                f"insert_pending_payment_log error: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (payment_id={payment_id})"
            )
            return None
        rows = resp.json()
        if not rows:
            logger.error(
                f"insert_pending_payment_log: empty response for {payment_id}"
            )
            return None
        return rows[0].get("id")
    except Exception as e:
        logger.error(
            f"insert_pending_payment_log failure (payment_id={payment_id}): {e}"
        )
        return None


async def update_payment_log_state(
    payment_id: str,
    state: str,
    *,
    expected_state: Optional[str] = None,
    **fields,
) -> None:
    """UPDATE payment_logs SET state = $1, [**fields] WHERE payment_id = $2.

    The set_updated_at_payment_logs trigger handles updated_at automatically.
    Common fields callers will pass:
        agent_address, tx_hash    — when the payment header arrives
        gateway_fee_usdc          — when the split fires
        refund_tx_hash            — when refund settles (#12)
        error_reason              — on failures
        client_ip, user_agent     — populate late if they weren't at insert time

    expected_state: optional WHERE filter. When provided, the
    PATCH only lands if the row's current state matches. This is the
    fix for the race where a fire-and-forget intermediate PATCH
    (e.g. 'verified') could arrive AFTER the awaited terminal PATCH
    ('payment_done') and overwrite it. With expected_state='pending'
    on the 'verified' PATCH, the racing-late case becomes a silent
    no-op (WHERE doesn't match) instead of corrupting the row.

    Idempotent — calling with the same (payment_id, state) twice is safe.
    """
    if not sb_enabled():
        return
    payload = {"state": state}
    for key, val in fields.items():
        # Skip None values so the caller can't accidentally null a column
        # by passing field=None.
        if val is not None:
            payload[key] = val

    params = {"payment_id": f"eq.{payment_id}"}
    if expected_state is not None:
        params["state"] = f"eq.{expected_state}"

    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params=params,
                json=payload,
            )
        if resp.status_code not in (200, 204):
            logger.error(
                f"update_payment_log_state error: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (payment_id={payment_id}, state={state})"
            )
    except Exception as e:
        logger.error(
            f"update_payment_log_state failure "
            f"(payment_id={payment_id}, state={state}): {e}"
        )


async def mark_split_failed(payment_id: str, reason: str) -> None:
    """Durably flag a payment whose revenue split could not be settled.

    split_payment() runs concurrently with (and usually finishes after) the
    route's terminal 'payment_done' PATCH, so we must NOT touch the `state`
    column — clobbering 'payment_done' would corrupt the funnel analytics and
    could be re-read as a non-terminal row. Instead we stamp `error_reason`
    only, leaving `state` untouched. A permanently-failed split is then
    reconcilable with:

        SELECT payment_id, developer_address, amount_usdc
          FROM payment_logs
         WHERE error_reason LIKE 'split_failed:%';

    No expected_state guard (unlike the refund helpers) precisely because we
    want this to land on whatever terminal state the row already holds. The
    error_reason column is otherwise unused on the happy path (it's only set
    on the tool-failure/refund branches, which are mutually exclusive with a
    successful paid call that owes a split). Best-effort: a failure to record
    the marker is logged but never raised — the caller is already in a
    degraded path.
    """
    if not sb_enabled():
        return
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={"payment_id": f"eq.{payment_id}"},
                json={"error_reason": f"split_failed: {reason}"[:300]},
            )
        if resp.status_code not in (200, 204):
            logger.error(
                f"mark_split_failed error: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (payment_id={payment_id})"
            )
    except Exception as e:
        logger.error(f"mark_split_failed failure (payment_id={payment_id}): {e}")


# Abandoned-pending sweep window. A pending payment_logs row is considered
# abandoned if it's been sitting in `pending` for longer than this without
# ever transitioning to `verified`. Matches the design doc §5.4 spec.
#
# 5 min is chosen to be 2.5× the 2-min payment_challenge TTL, so a slow
# agent that pays right at the TTL boundary doesn't get its row swept
# before verify completes.
_ABANDONED_AFTER_SECONDS = 5 * 60


async def correlate_pending_challenge(
    tool_name: str,
    client_ip: Optional[str],
    user_agent: Optional[str],
    tx_hash: str,
) -> Optional[str]:
    """Best-effort: link a Base/free-v2 settle back to the 402 that prompted it.

    WHY THIS EXISTS. x402-v2 does not echo our UUID back through
    PAYMENT-SIGNATURE, so a Base settle is keyed on tx_hash and writes a
    SECOND row; the original UUID-keyed pending row is never touched and the
    sweep marks it 'abandoned'. Every success therefore mints a phantom
    abandonment, and no 402 can be tied to its own settlement.

    Two consequences, one small and one not:
      * Inflation is currently trivial (~311 phantoms vs ~121k abandoned rows
        = 0.26%). This is NOT why the funnel looks bad.
      * But `conversion = payment_done / (payment_done + abandoned)` is
        SYSTEMATICALLY WRONG, and the error scales with success: at a true 50%
        conversion the query reports 33%. It corrupts the metric precisely when
        the metric starts to matter. That's the reason to fix it now, cheaply,
        rather than when there's revenue riding on the number.

    Correlation is HEURISTIC and best-effort. There is no shared key, so we
    match the most recent still-pending row on (tool_name, client_ip,
    user_agent) inside the sweep window. Caveats, stated plainly:
      * client_ip is near-useless as a discriminator — Railway's edge puts
        almost everything on 100.64.0.x (CGNAT). It's kept as a weak filter,
        not a identity.
      * UA is not unique either: many distinct clients share bare 'node'.
      * So under concurrency this CAN attribute a settle to the wrong client's
        challenge. That is acceptable ONLY because this column is analytics,
        never money: nothing about verification, replay, or the split reads it.

    Failure degrades to exactly today's behaviour (row → 'abandoned' via the
    sweep), so this is safe to run fire-and-forget off the hot path.

    Returns the correlated payment_id, or None if nothing matched.
    """
    if not sb_enabled():
        return None
    try:
        from datetime import timedelta
        since = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=_ABANDONED_AFTER_SECONDS)
        ).isoformat()
        params = {
            "select":     "payment_id",
            "state":      "eq.pending",
            "tool_name":  f"eq.{tool_name}",
            "created_at": f"gte.{since}",
            "order":      "created_at.desc",
            "limit":      "1",
        }
        # Weak filters — only applied when present, so a UA-less client still
        # correlates on (tool_name, window) rather than not at all.
        if client_ip:
            params["client_ip"] = f"eq.{client_ip}"
        if user_agent:
            params["user_agent"] = f"eq.{user_agent}"

        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params=params,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"correlate_pending_challenge lookup: HTTP {resp.status_code} "
                    f"body={resp.text[:120]}"
                )
                return None
            rows = resp.json() or []
            if not rows:
                return None
            pid = rows[0].get("payment_id")
            if not pid:
                return None

            # 'superseded' — the challenge WAS answered; the outcome lives on
            # the tx-keyed row. Deliberately NOT 'payment_done': that would
            # double-count successes in the very query this exists to fix.
            # Deliberately not a DELETE either: the row's created_at is the
            # 402-issue time, so keeping it gives time-to-pay for free.
            patch = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={"payment_id": f"eq.{pid}", "state": "eq.pending"},
                json={"state": "superseded", "tx_hash": tx_hash},
            )
            if patch.status_code not in (200, 204):
                logger.warning(
                    f"correlate_pending_challenge patch: HTTP {patch.status_code} "
                    f"body={patch.text[:120]} (payment_id={pid})"
                )
                return None
            logger.info(
                f"[FUNNEL] correlated 402 {str(pid)[:8]}… → settle {str(tx_hash)[:14]}… "
                f"tool={tool_name}"
            )
            return pid
    except Exception as e:
        # Never let analytics break a paid call that already settled on-chain.
        logger.warning(f"correlate_pending_challenge failure (tool={tool_name}): {e}")
        return None


async def sweep_abandoned_pending() -> int:
    """Transition stale pending payment_logs rows to state='abandoned'.

    PATCH payment_logs SET state='abandoned' WHERE state='pending'
    AND created_at < now() - interval '5 minutes'.

    Returns the count of rows transitioned, or 0 on error / Supabase
    disabled. Called from the periodic _abandoned_sweep_loop task in
    main.py:lifespan.

    Unlike cleanup_expired_challenges (which DELETEs from the transient
    pending_challenges lookup table), this PATCHes payment_logs in
    place — the abandoned row stays as a permanent analytics record.
    The conversion-by-tool query in §5.5 of the design doc relies on
    counting abandoned vs. payment_done rows per tool.

    IMPORTANT (2026-07-17): 'abandoned' means "we issued a 402 and NOTHING
    came back" — it does NOT mean "tried and failed". A client that answers
    with a bad payload is 'rejected' with an error_reason; a client whose
    answer settled on Base is 'superseded' via correlate_pending_challenge.
    Historically neither had ever fired: 0 'rejected' rows and 0 non-null
    error_reason in the entire table, i.e. no client has ever sent a payload
    we refused. Abandonment here is silence, not failure — don't read it as a
    payments bug.

    Because a Base settle is keyed on tx_hash (x402-v2 doesn't echo our UUID),
    the §5.5 conversion query MUST exclude 'superseded' from the denominator
    or it double-counts every success as an abandonment too:

        conversion = payment_done / (payment_done + abandoned)
          -- 'superseded' rows are answered challenges; excluding them is the
          -- whole point. Before correlate_pending_challenge existed they were
          -- silently mixed into 'abandoned' and the ratio was wrong by
          -- construction, with the error scaling as success grew.
    """
    if not sb_enabled():
        return 0
    try:
        from datetime import timedelta
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=_ABANDONED_AFTER_SECONDS)
        ).isoformat()
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                # return=representation echoes deleted rows so we can count
                headers={**sb_headers(), "Prefer": "return=representation"},
                params={
                    "state":      "eq.pending",
                    "created_at": f"lt.{cutoff}",
                },
                json={"state": "abandoned"},
            )
        if resp.status_code not in (200, 204):
            logger.error(
                f"sweep_abandoned_pending error: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
            return 0
        if resp.status_code == 200:
            return len(resp.json())
        return 0
    except Exception as e:
        logger.error(f"sweep_abandoned_pending failure: {e}")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Refund worker
# ─────────────────────────────────────────────────────────────────────────────
#
# When a paid tool call fails post-verify, routes/tools.py:call_tool sets
# payment_logs.state='refund_pending'. This group exposes the ORM that the
# background _refund_worker_loop in main.py:lifespan uses to drive each
# row through the rest of the lifecycle:
#
#   refund_pending ──send_refund OK──→ refund_done           (terminal happy)
#                  ──send_refund fails (1..4 attempts)──→ refund_pending
#                  ──send_refund fails (5th attempt)────→ refund_failed (terminal)
#
# Retry count is persisted in the refund_attempts column.
# Cap is 5 attempts; at 60s/attempt that's ~5 min of retry window per row.
#
# Behaviour conventions:
#   claim_refund_pending() — read, returns the actual rows so the worker
#       has all fields needed for send_refund (agent_address, amount_usdc,
#       network, payment_id). Filtered to refund_attempts < cap so the
#       failed-out rows don't get re-tried.
#   increment_refund_attempt() — write, atomic increment of the counter
#       using Postgres-side arithmetic via PostgREST.
#   mark_refund_done() — write, terminal state transition with the refund
#       tx_hash. State-guarded against double-write.
#   mark_refund_failed() — write, terminal state for retry exhaustion.
#       Accepts both 'refund_pending' AND 'refund_failed' as expected
#       state so a retry of the terminal write is a no-op rather than
#       a 0-row update that the caller can't distinguish from a bug.

_REFUND_ATTEMPT_CAP = 5


async def claim_refund_pending(limit: int = 20) -> list[dict]:
    """SELECT rows in state='refund_pending' with attempts < cap.

    Returns up to `limit` rows ordered by created_at ASC (oldest first)
    so each pass of the worker makes monotonic progress. Empty list on
    Supabase error / disabled — the worker treats that as "nothing to
    do this tick" and waits for the next sweep.

    No locking. Multi-pod deploys would re-claim the same rows; we run
    single-pod on Railway today and Stellar's submit_transaction is
    idempotent enough that a double-send is a survivable duplicate
    transfer (the agent receives twice — manual reconciliation, but
    no funds lost).
    """
    if not sb_enabled():
        return []
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={
                    "select":          "id,payment_id,agent_address,amount_usdc,network,tool_name,refund_attempts",
                    "state":           "eq.refund_pending",
                    "refund_attempts": f"lt.{_REFUND_ATTEMPT_CAP}",
                    "order":           "created_at.asc",
                    "limit":           str(limit),
                },
            )
        if resp.status_code != 200:
            logger.error(
                f"claim_refund_pending error: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
            return []
        return resp.json()
    except Exception as e:
        logger.error(f"claim_refund_pending failure: {e}")
        return []


async def sweep_cap_exhausted_refunds() -> int:
    """Terminal-state a leaked row class (AGE-61 follow-up): rows sitting in
    state='refund_pending' with refund_attempts >= cap. They fall outside
    claim_refund_pending's `refund_attempts < cap` filter, so without this
    sweep they stay pending forever, invisible to the worker AND to failure
    analytics. Reachable two ways: a worker crash between increment and
    mark_*, or (new with the confirmed-increment contract) repeated blips
    that burn attempts without a send.

    PATCHes them to refund_failed with error_reason='cap_exhausted_no_send'.
    Returns the number of rows transitioned (0 on error/disabled).
    """
    if not sb_enabled():
        return 0
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers={**sb_headers(), "Prefer": "return=representation"},
                params={
                    "state":           "eq.refund_pending",
                    "refund_attempts": f"gte.{_REFUND_ATTEMPT_CAP}",
                },
                json={"state": "refund_failed",
                      "error_reason": "cap_exhausted_no_send"},
            )
        if resp.status_code != 200:
            logger.error(
                f"sweep_cap_exhausted_refunds error: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
            return 0
        n = len(resp.json())
        if n:
            logger.warning(
                f"sweep_cap_exhausted_refunds: {n} rows → refund_failed "
                f"(cap reached without a completed send)"
            )
        return n
    except Exception as e:
        logger.error(f"sweep_cap_exhausted_refunds failure: {e}")
        return 0


async def increment_refund_attempt(payment_id: str) -> int | None:
    """PATCH refund_attempts = refund_attempts + 1 (read-modify-write).

    PostgREST doesn't expose SQL-side arithmetic in a PATCH body
    directly — but we can hit a SQL function via /rpc, or read-modify-
    write. We use read-modify-write here for simplicity: the row's
    state filter (state=refund_pending) plus the single-worker
    invariant means the increment is effectively serial. If we ever
    scale to multiple workers, switch to an `inc_refund_attempt`
    Postgres function exposed via /rpc.

    Called BEFORE the on-chain send so a worker crash mid-attempt
    still counts against the cap — bias towards "don't retry forever"
    over "don't waste an attempt".

    Returns (AGE-61):
        int  — the NEW attempt count, confirmed written.
        None — the increment could NOT be confirmed (read failed, row
               missing, or write failed). Callers MUST NOT send a refund
               on None: the old behaviour defaulted a failed read to
               current=0, so a single read blip reset refund_attempts
               from e.g. 4 back to 1 and let the worker blow past the
               5-attempt cap — and every attempt is a real USDC send.
    """
    if not sb_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            # Read current count — a failed or empty read is a hard stop,
            # never "assume 0" (AGE-61).
            r = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={
                    "payment_id": f"eq.{payment_id}",
                    "select":     "refund_attempts",
                },
            )
            if r.status_code != 200 or not r.json():
                logger.error(
                    f"increment_refund_attempt read failed: HTTP {r.status_code} "
                    f"rows={len(r.json()) if r.status_code == 200 else 'n/a'} "
                    f"(payment_id={payment_id}) — attempt NOT authorized"
                )
                return None
            current = int(r.json()[0].get("refund_attempts", 0) or 0)
            # Write +1
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={"payment_id": f"eq.{payment_id}"},
                json={"refund_attempts": current + 1},
            )
        if resp.status_code not in (200, 204):
            logger.error(
                f"increment_refund_attempt write failed: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (payment_id={payment_id}) — attempt NOT authorized"
            )
            return None
        return current + 1
    except Exception as e:
        logger.error(
            f"increment_refund_attempt failure (payment_id={payment_id}): {e} "
            f"— attempt NOT authorized"
        )
        return None


async def claim_refund_sending(payment_id: str) -> bool:
    """AGE-76 two-phase claim: refund_pending → refund_sending, CONFIRMED.

    The USDC send is authorized ONLY by a confirmed claim: with the row in
    'refund_sending', claim_refund_pending can never re-claim it blind, so a
    send whose terminal PATCH later fails cannot be silently re-sent. Stale
    'refund_sending' rows are resolved by the worker's stale sweep via the
    on-chain memo idempotency check.

    Returns True only when exactly this transition landed (1 row). False on
    any error, or when the row was not in refund_pending — the caller MUST
    NOT send on False.
    """
    if not sb_enabled():
        return False
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers={**sb_headers(), "Prefer": "return=representation"},
                params={
                    "payment_id": f"eq.{payment_id}",
                    "state":      "eq.refund_pending",
                },
                json={"state": "refund_sending"},
            )
        if resp.status_code != 200:
            logger.error(
                f"claim_refund_sending error: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (payment_id={payment_id})"
            )
            return False
        return len(resp.json()) == 1
    except Exception as e:
        logger.error(f"claim_refund_sending failure (payment_id={payment_id}): {e}")
        return False


async def release_refund_sending(payment_id: str) -> None:
    """AGE-76: after a FAILED send, put the row back in the retry pool
    (refund_sending → refund_pending; the attempt was already counted).
    Best-effort — if this PATCH fails the row stays in refund_sending and
    the stale sweep resolves it via the on-chain check."""
    await update_payment_log_state(
        payment_id,
        "refund_pending",
        expected_state="refund_sending",
    )


async def list_refund_sending() -> list[dict]:
    """AGE-76 stale sweep input: every row currently in 'refund_sending'.
    The worker resolves each via the on-chain memo idempotency check —
    at sweep start, any such row is from a crashed/blipped earlier sweep
    (this sweep's claims happen after)."""
    if not sb_enabled():
        return []
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={
                    "state":  "eq.refund_sending",
                    "select": "payment_id,agent_address,amount_usdc,network,refund_attempts",
                },
            )
        if resp.status_code != 200:
            logger.error(f"list_refund_sending error: HTTP {resp.status_code}")
            return []
        return resp.json()
    except Exception as e:
        logger.error(f"list_refund_sending failure: {e}")
        return []


async def mark_refund_done(payment_id: str, refund_tx_hash: str) -> None:
    """Terminal happy-path transition. PATCH state='refund_done',
    refund_tx_hash=$1, guarded to the in-flight states so we don't
    accidentally overwrite a refund_failed (which would happen if a
    stale worker comes back after we'd already given up).

    AGE-76: the row is in 'refund_sending' when a send just completed
    (two-phase claim); 'refund_pending' is kept in the guard for
    backward compatibility with rows written before the deploy.
    """
    if not sb_enabled():
        return
    payload = {"state": "refund_done", "refund_tx_hash": refund_tx_hash}
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={
                    "payment_id": f"eq.{payment_id}",
                    "state":      "in.(refund_sending,refund_pending)",
                },
                json=payload,
            )
        if resp.status_code not in (200, 204):
            logger.error(
                f"mark_refund_done error: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (payment_id={payment_id})"
            )
    except Exception as e:
        logger.error(f"mark_refund_done failure (payment_id={payment_id}): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Flagship runs (public ledger reasoning)
# ─────────────────────────────────────────────────────────────────────────────
#
# The flagship analyst agent POSTs a full run summary after each daily run
# (POST /v1/flagship/run). It is stored here so /ledger can render WHY each call
# happened — the plan estimate, regime read, per-verdict factor breakdown, and
# the spending receipt — not just the on-chain payment_logs rows. One row per run.
#
# Table: flagship_runs (see db/migrate.py FLAGSHIP_RUNS_DDL). JSONB columns hold
# the structured plan/verdicts/receipt/free_intel; run_at is the agent's run
# timestamp (used to merge with the payment_logs-grouped runs on the ledger).
#
# Behaviour: best-effort. A missing table or Supabase blip logs + returns
# False/[] so the ingest endpoint stays a no-op rather than failing the agent.


async def insert_flagship_run(run: dict) -> bool:
    """INSERT one flagship run summary, idempotent on run_at. Returns True on
    success.

    `run` is the agent's posted payload; only known columns are forwarded.

    AGE-63: run_at is the idempotency key. The analyst cron posts once per run,
    but a retried/duplicated POST used to INSERT a second row → the same run
    appeared twice on /ledger and double-counted headline totals. We now skip
    the insert when a row for this run_at already exists (first-write-wins), so
    a retry of the same run is a no-op on the totals and returns success.

    Chosen over delete-then-insert deliberately: a delete that succeeds before
    a failing insert would LOSE an already-stored run, whereas check-then-skip
    has no data-loss window. Schema-agnostic — keys on the run_at column, needs
    no UNIQUE constraint (a Postgres UNIQUE(run_at) + native upsert is the tidy
    long-term form, and would additionally let a corrected re-post replace).
    Rows with no run_at skip the existence check (can't idempotency-key them)
    and insert as before.
    """
    if not sb_enabled():
        return False
    run_at = run.get("run_at_iso") or run.get("run_at")
    payload = {
        "run_at":     run_at,
        "wallet":     run.get("wallet"),
        "max_spend":  run.get("max_spend"),
        "objective":  run.get("objective") or {},
        "plan":       run.get("plan") or {},
        "regime":     run.get("regime") or "",
        "context":    run.get("context") or "",
        "verdicts":   run.get("verdicts") or {},
        "skipped":    run.get("skipped") or {},
        "findings":   run.get("findings") or {},
        "receipt":    run.get("receipt") or {},
        "free_intel": run.get("free_intel") or {},
        "note":       run.get("note") or "",
    }
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            if run_at:
                # Idempotency guard: if this run_at is already stored, treat the
                # (re-)post as a successful no-op instead of inserting a dup.
                exist = await client.get(
                    f"{settings.SUPABASE_URL}/rest/v1/flagship_runs",
                    headers={**sb_headers(), "Accept": "application/json"},
                    params={"run_at": f"eq.{run_at}", "select": "run_at", "limit": "1"},
                )
                if exist.status_code == 200 and exist.json():
                    logger.info(
                        f"insert_flagship_run: run_at={run_at!r} already stored "
                        f"— idempotent no-op"
                    )
                    return True
                # A failed existence check is non-fatal: fall through and insert
                # (worst case reverts to the pre-AGE-63 possible-duplicate, never
                # a lost or blocked run).
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/flagship_runs",
                headers=sb_headers(),
                json=payload,
            )
        if resp.status_code not in (200, 201, 204):
            logger.error(
                f"insert_flagship_run error: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
            return False
        return True
    except Exception as e:
        logger.error(f"insert_flagship_run failure: {e}")
        return False


async def fetch_flagship_runs(limit: int = 200) -> list[dict]:
    """SELECT flagship run summaries, newest first. [] on error/disabled/missing."""
    if not sb_enabled():
        return []
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/flagship_runs",
                headers={**sb_headers(), "Accept": "application/json"},
                params={
                    "select": "run_at,wallet,max_spend,objective,plan,regime,context,"
                              "verdicts,skipped,findings,receipt,free_intel,note",
                    "order":  "run_at.desc",
                    "limit":  str(limit),
                },
            )
        if resp.status_code != 200:
            # 404 = table not created yet; degrade silently to no reasoning.
            if resp.status_code != 404:
                logger.error(f"fetch_flagship_runs error: HTTP {resp.status_code}")
            return []
        return resp.json()
    except Exception as e:
        logger.error(f"fetch_flagship_runs failure: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Active Prober — service_probes / service_scores (AGE-6)
# ─────────────────────────────────────────────────────────────────────────────
#
# Tables: db/migrations/service_probes.sql. Raw probes are PRIVATE (evidence
# for negative flags: tx hash + error snapshot); scores are PUBLIC-read.
# Written by the gateway on POST /v1/prober/run (the prober itself is a
# credential-free HTTP customer, same pattern as the flagship ingest).
#
# Behaviour: best-effort. A missing table or Supabase blip logs + returns
# False/[] so a prober ingest never hard-fails over storage.

# Columns forwarded to service_probes — anything else in a posted row is
# dropped (the runner also carries name/skipped fields the table doesn't).
_PROBE_COLUMNS = (
    "probed_at", "resource_url", "name", "need", "pay_to", "network", "price_usdc",
    "probe_type", "alive", "x402_wellformed", "price_matches", "mpp_option",
    "usdg_option",
    "settle_ok", "http_ok", "latency_ms", "response_nonempty", "schema_ok",
    "tx_hash", "error",
)

_SCORE_COLUMNS = (
    "resource_url", "name", "need", "network", "window_days", "paid_probes", "delivery_rate",
    "delivery_factor", "latency_p50_ms", "last_ok_at", "last_fail_at", "flags",
    "mpp_option", "usdg_option", "price_usdc",
)


async def insert_service_probes(rows: list[dict]) -> bool:
    """Bulk-INSERT raw probe rows. Returns True on success."""
    if not rows:
        return True          # nothing to write = vacuous success
    if not sb_enabled():
        return False
    payload = [{k: r.get(k) for k in _PROBE_COLUMNS} for r in rows
               if r.get("resource_url")]
    if not payload:
        return False
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/service_probes",
                headers=sb_headers(),
                json=payload,
            )
        if resp.status_code not in (200, 201, 204):
            logger.error(f"insert_service_probes error: HTTP {resp.status_code} "
                         f"body={resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"insert_service_probes failure: {e}")
        return False


async def fetch_service_probes(window_days: int = 30, limit: int = 5000) -> list[dict]:
    """SELECT probe rows inside the scoring window, newest first.
    [] on error/disabled/missing — the caller then scores this run's rows only."""
    if not sb_enabled():
        return []
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/service_probes",
                headers={**sb_headers(), "Accept": "application/json"},
                params={
                    "select":    ",".join(_PROBE_COLUMNS),
                    "probed_at": f"gte.{cutoff}",
                    "order":     "probed_at.desc",
                    "limit":     str(limit),
                },
            )
        if resp.status_code != 200:
            if resp.status_code != 404:   # 404 = table not created yet
                logger.error(f"fetch_service_probes error: HTTP {resp.status_code}")
            return []
        return resp.json()
    except Exception as e:
        logger.error(f"fetch_service_probes failure: {e}")
        return []


async def upsert_service_scores(rows: list[dict]) -> bool:
    """UPSERT score rows on resource_url (merge-duplicates). Returns True on
    success. updated_at is stamped here, not by the caller."""
    if not sb_enabled() or not rows:
        return False
    now = datetime.now(timezone.utc).isoformat()
    payload = [{**{k: r.get(k) for k in _SCORE_COLUMNS}, "updated_at": now}
               for r in rows if r.get("resource_url")]
    if not payload:
        return False
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/service_scores",
                headers={**sb_headers(),
                         "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "resource_url"},
                json=payload,
            )
        if resp.status_code not in (200, 201, 204):
            logger.error(f"upsert_service_scores error: HTTP {resp.status_code} "
                         f"body={resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"upsert_service_scores failure: {e}")
        return False


def _group_paid_receipts(rows: list[dict]) -> list[dict]:
    """Pure: group payment_logs rows into per-tool paid-call evidence,
    keeping ONLY genuinely paid rows (Decimal(amount) > 0).

    amount_usdc is written to Supabase as a *string* (see record_payment),
    so a PostgREST `amount_usdc=gt.0` filter compares TEXT — "0.000000" >
    "0" lexicographically — and lets every $0 free-flow receipt through
    (free tools traverse the full x402 lifecycle into payment_logs by
    design). The authoritative paid/free split therefore happens HERE, in
    Python, with a real Decimal comparison. Unparseable amounts are
    treated as unpaid (excluded). Rows are expected newest-first; the
    first row seen per tool provides last_paid_at. AGE-38."""
    from decimal import Decimal, InvalidOperation
    by_tool: dict[str, dict] = {}
    for r in rows:
        t = r.get("tool_name")
        if not t:
            continue
        try:
            if Decimal(str(r.get("amount_usdc") or "0")) <= 0:
                continue
        except (InvalidOperation, ValueError):
            continue
        row = by_tool.setdefault(t, {"tool": t, "paid_calls": 0,
                                     "last_paid_at": r.get("created_at")})
        row["paid_calls"] += 1
    return sorted(by_tool.values(), key=lambda r: -r["paid_calls"])


async def fetch_own_tool_receipts() -> list[dict]:
    """Per-tool receipt evidence for AgentPay's own PAID tools, from
    payment_logs (state=payment_done, amount > 0). Powers the /probes
    self-section: our delivery proof is real customers' on-chain receipts,
    never self-probes. [] on error/disabled.

    NOTE: the server-side `amount_usdc=gt.0` filter is best-effort only
    (text column — see _group_paid_receipts); it never drops a paid row
    but does NOT reliably drop free ones. _group_paid_receipts is the
    authoritative filter."""
    if not sb_enabled():
        return []
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers={**sb_headers(), "Accept": "application/json"},
                params={
                    "select":      "tool_name,created_at,amount_usdc",
                    "state":       "eq.payment_done",
                    "amount_usdc": "gt.0",
                    "order":       "created_at.desc",
                    "limit":       "2000",
                },
            )
        if resp.status_code != 200:
            logger.error(f"fetch_own_tool_receipts error: HTTP {resp.status_code}")
            return []
        return _group_paid_receipts(resp.json())
    except Exception as e:
        logger.error(f"fetch_own_tool_receipts failure: {e}")
        return []


async def fetch_service_scores() -> dict[str, dict]:
    """SELECT all score rows keyed by resource_url — the input dict decide()
    joins on (AGE-7). {} on error/disabled/missing (decide() then treats every
    service as unprobed = neutral factor 1.0)."""
    if not sb_enabled():
        return {}
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/service_scores",
                headers={**sb_headers(), "Accept": "application/json"},
                params={"select": ",".join(_SCORE_COLUMNS) + ",updated_at"},
            )
        if resp.status_code != 200:
            if resp.status_code != 404:
                logger.error(f"fetch_service_scores error: HTTP {resp.status_code}")
            return {}
        return {r["resource_url"]: r for r in resp.json() if r.get("resource_url")}
    except Exception as e:
        logger.error(f"fetch_service_scores failure: {e}")
        return {}


async def mark_refund_failed(payment_id: str, error_reason: str) -> None:
    """Terminal sad-path transition after cap exhaustion. Filters by
    expected_state IN ('refund_pending', 'refund_sending', 'refund_failed')
    so a retry of this terminal write is idempotent — second call lands as
    a no-op rather than a 0-rows update that callers can't distinguish
    from a bug. ('refund_sending' added with AGE-76's two-phase claim: the
    cap-exhaustion write now happens while the row is claimed.)

    PostgREST 'in.(...)' syntax for the state filter.
    """
    if not sb_enabled():
        return
    payload = {"state": "refund_failed", "error_reason": error_reason}
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={
                    "payment_id": f"eq.{payment_id}",
                    "state":      "in.(refund_pending,refund_sending,refund_failed)",
                },
                json=payload,
            )
        if resp.status_code not in (200, 204):
            logger.error(
                f"mark_refund_failed error: HTTP {resp.status_code} "
                f"body={resp.text[:200]} (payment_id={payment_id})"
            )
    except Exception as e:
        logger.error(
            f"mark_refund_failed failure (payment_id={payment_id}): {e}"
        )
