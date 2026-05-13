"""
services/supabase.py — Supabase REST helpers.

Wraps the Supabase REST API in raw httpx — works with the sb_secret_ key
format that the supabase-py SDK can't handle.

PR #13 expanded this from the original log_payment helper into the
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
# in PR #14. The current pattern is:
#   1. insert_pending_payment_log() at 402-issue time → state='pending' row
#   2. update_payment_log_state() at each lifecycle transition (verified,
#      split_done, payment_done, rejected, abandoned, refund_pending)
# See routes/tools.py:call_tool for the integration site, and §5 of the
# Tier 2 design doc for the state machine.


# ─────────────────────────────────────────────────────────────────────────────
# Replay protection (PR #13 Group 1)
# ─────────────────────────────────────────────────────────────────────────────
#
# Two tables, both insert-only:
#   replay_payment_ids — UUID side. PK is payment_id alone.
#   replay_tx_hashes   — hash side. Composite PK on (tx_hash, network) so
#                        a Stellar testnet hash can't collide with a Base
#                        mainnet hash.
#
# Behaviour conventions for this group:
#   record_*()  — returns True on successful insert, False if row already
#                 exists (HTTP 409). On other errors (network, 5xx),
#                 log at error level and return True (don't block on
#                 Supabase issues; in-memory is still primary).
#   is_*_consumed() — returns True if row exists in DB, False otherwise.
#                 On error, return False (assume not consumed; in-memory
#                 dedupe will catch it).
#
# Reads are NOT called during the dual-write phase (#13). They become
# active during cutover (#13 row 7) when Supabase becomes primary.


async def record_payment_id(payment_id: str) -> bool:
    """Insert payment_id into replay_payment_ids.

    Returns:
        True  — newly recorded (or Supabase unreachable, treated as success
                so we don't block legitimate payments)
        False — already consumed (HTTP 409 conflict)
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
            return False  # already consumed
        if resp.status_code not in (200, 201):
            logger.error(
                f"record_payment_id Supabase error: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
        return True
    except Exception as e:
        logger.error(f"record_payment_id Supabase failure (payment_id={payment_id}): {e}")
        return True  # don't block on infrastructure errors


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


async def record_tx_hash(tx_hash: str, network: str) -> bool:
    """Insert (tx_hash, network) into replay_tx_hashes.

    network must be one of: 'stellar-mainnet', 'stellar-testnet',
    'base-mainnet', 'base-sepolia'. Composite PK means the same hash can
    exist across networks (extremely unlikely, but defensive).

    Returns:
        True  — newly recorded (or Supabase unreachable)
        False — already consumed (HTTP 409 conflict)
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
            return False
        if resp.status_code not in (200, 201):
            logger.error(
                f"record_tx_hash Supabase error: HTTP {resp.status_code} "
                f"body={resp.text[:200]}"
            )
        return True
    except Exception as e:
        logger.error(
            f"record_tx_hash Supabase failure "
            f"(tx_hash={tx_hash[:16]}..., network={network}): {e}"
        )
        return True


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
# Pending challenges (PR #13 Group 2)
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
# Faucet IP cooldown (PR #13 Group 3)
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
# payment_logs lifecycle (PR #13 Group 4)
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
    where agent_address and tx_hash aren't known yet. PR #14 also uses
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

    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={"payment_id": f"eq.{payment_id}"},
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


# Abandoned-pending sweep window. A pending payment_logs row is considered
# abandoned if it's been sitting in `pending` for longer than this without
# ever transitioning to `verified`. Matches the design doc §5.4 spec.
#
# 5 min is chosen to be 2.5× the 2-min payment_challenge TTL, so a slow
# agent that pays right at the TTL boundary doesn't get its row swept
# before verify completes.
_ABANDONED_AFTER_SECONDS = 5 * 60


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
