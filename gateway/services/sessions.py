"""Server-side session cap: the gateway's own copy of the SDK's budget cap,
bound to the address that paid for `session_create`, so a standard x402
client with no AgentPay SDK still has a spend limit.

Every priced call reserves its price against the payer's live session
BEFORE anything settles (consume_session_budget, atomic in Postgres); a
refusal charges nothing. Release on a settle that charged nothing; keep the
reservation when the outcome is uncertain, since the payment may still land.
Off unless SESSION_ENFORCEMENT is set and Supabase is configured.
See db/migrations/sessions.sql for the tables and functions.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx

from gateway.config import settings
from gateway.services.supabase import sb_enabled, sb_headers

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0
# Rails where the gateway decides before money moves. Stellar pays first,
# so its calls are recorded against the session but never refused.
ENFORCED_RAILS = ("stacks", "base")

REFUSED_OVER_CAP = "session_cap_exceeded"
REFUSED_UNAVAILABLE = ("session_store_unavailable: the spend cap could not be "
                       "checked — nothing was charged, retry")


def enabled() -> bool:
    return bool(settings.SESSION_ENFORCEMENT) and sb_enabled()


def rail_of(network: str) -> str:
    return (network or "").split("-", 1)[0].split(":", 1)[0]


@dataclass
class Hold:
    """A reservation against a session, carried from settle to the terminal
    payment_logs write."""
    session_id: str
    cost: str
    enforced: bool
    max_spend: str = ""
    spent: str = ""
    refused: Optional[str] = None   # REFUSED_* when the call must not settle

    @property
    def remaining(self) -> str:
        try:
            return str(Decimal(self.max_spend) - Decimal(self.spent))
        except (InvalidOperation, ValueError):
            return ""


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clamp_ttl(ttl_seconds) -> int:
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        ttl = settings.SESSION_DEFAULT_TTL_S
    return max(60, min(ttl, settings.SESSION_MAX_TTL_S))


async def _rpc(name: str, args: dict) -> Optional[list]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/rpc/{name}",
                headers=sb_headers(), json=args,
            )
        if resp.status_code != 200:
            logger.error(f"[SESSION] rpc {name} HTTP {resp.status_code} body={resp.text[:200]}")
            return None
        data = resp.json()
        return data if isinstance(data, list) else [data]
    except Exception as e:
        logger.error(f"[SESSION] rpc {name} failed: {e}")
        return None


async def open_session(*, payer: str, network: str, max_spend: str,
                       label: Optional[str], payment_id: str,
                       ttl_seconds=None) -> Optional[dict]:
    """Persist a session for the address that just paid. When that address
    already holds an active session, return it unchanged — the payer keeps
    its cap and the new create does not raise it."""
    if not enabled() or not payer:
        return None
    try:
        cap = str(Decimal(str(max_spend)))
    except (InvalidOperation, ValueError):
        cap = "0.10"
    now = datetime.now(tz=timezone.utc)
    row = {
        "session_id": str(uuid.uuid4()),
        "payer": payer,
        "network": network,
        "max_spend": cap,
        "spent": "0",
        "status": "active",
        "label": label,
        "payment_id": payment_id,
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=clamp_ttl(ttl_seconds))),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/sessions",
                headers={**sb_headers(), "Prefer": "return=representation"},
                json=row,
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            created = data[0] if isinstance(data, list) and data else row
            return {**created, "reused": False}
        if resp.status_code == 409:
            existing = await active_for(payer)
            if existing is not None:
                return {**existing, "reused": True}
        logger.error(f"[SESSION] create failed HTTP {resp.status_code} body={resp.text[:200]}")
    except Exception as e:
        logger.error(f"[SESSION] create failed: {e}")
    return None


async def active_for(payer: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/sessions",
                headers=sb_headers(),
                params={"payer": f"eq.{payer}", "status": "eq.active",
                        "order": "created_at.desc", "limit": "1"},
            )
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]
    except Exception as e:
        logger.error(f"[SESSION] lookup failed: {e}")
    return None


async def get_session(session_id: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/sessions",
                headers=sb_headers(),
                params={"session_id": f"eq.{session_id}", "limit": "1"},
            )
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]
    except Exception as e:
        logger.error(f"[SESSION] get failed: {e}")
    return None


async def receipts_for(session_id: str, limit: int = 200) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                params={"session_id": f"eq.{session_id}",
                        "select": "payment_id,tool_name,network,amount_usdc,state,tx_hash,created_at",
                        "order": "created_at.desc", "limit": str(limit)},
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"[SESSION] receipts failed: {e}")
    return []


async def reserve(payer: str, cost: str, network: str,
                  session_id: Optional[str] = None) -> Optional[Hold]:
    """Reserve `cost` for a priced call from `payer`.

    None → enforcement off or no live session for this payer (unmetered).
    Hold with refused=None → reserved; carry it to the terminal write.
    Hold with refused set → on an enforced rail the call must not settle;
    on Stellar the caller settles anyway and records the overrun.
    """
    if not enabled() or not payer:
        return None
    enforced = rail_of(network) in ENFORCED_RAILS
    rows = await _rpc("consume_session_budget",
                      {"p_payer": payer, "p_cost": str(cost), "p_session_id": session_id})
    if rows is None:
        # The replay store already fails paid calls closed on a Supabase
        # outage; the cap check follows the same rule on enforced rails.
        return Hold(session_id=session_id or "", cost=str(cost), enforced=enforced,
                    refused=REFUSED_UNAVAILABLE if enforced else None)
    r = rows[0] if rows else {}
    if not r.get("r_found"):
        return None
    hold = Hold(session_id=str(r.get("r_session_id") or ""), cost=str(cost), enforced=enforced,
                max_spend=str(r.get("r_max_spend") or ""), spent=str(r.get("r_spent") or ""))
    if not r.get("r_ok"):
        hold.refused = REFUSED_OVER_CAP
        logger.info(f"[SESSION] {hold.session_id[:8]}… refused {cost} "
                    f"(spent {hold.spent}/{hold.max_spend}, enforced={enforced})")
    return hold


async def release(hold: Optional[Hold]) -> None:
    """Give back a reservation after a settle that charged nothing."""
    if hold is None or hold.refused or not hold.session_id:
        return
    rows = await _rpc("release_session_budget",
                      {"p_session_id": hold.session_id, "p_cost": hold.cost})
    if rows is None:
        logger.critical(f"[ALERT] session {hold.session_id[:8]}… reservation {hold.cost} "
                        f"not released — cap over-counts until corrected")


async def release_by_id(session_id: str, cost: str) -> None:
    """Release for a reservation only known from a payment_logs row (a
    redeemed-uncertain settle that turned out dropped)."""
    if not enabled() or not session_id:
        return
    await release(Hold(session_id=session_id, cost=str(cost), enforced=True))


async def expire_stale() -> int:
    if not enabled():
        return 0
    rows = await _rpc("expire_sessions", {})
    if not rows:
        return 0
    try:
        return int(rows[0] if not isinstance(rows[0], dict) else next(iter(rows[0].values())))
    except (TypeError, ValueError, StopIteration):
        return 0


def refusal_body(hold: Hold, tool_name: str) -> dict:
    body = {
        "error": "Session spend cap reached" if hold.refused == REFUSED_OVER_CAP
                 else "Session cap check unavailable",
        "reason": hold.refused,
        "tool": tool_name,
        "session_id": hold.session_id or None,
        "charged": "0",
    }
    if hold.refused == REFUSED_OVER_CAP:
        body.update({"max_spend": hold.max_spend, "spent": hold.spent,
                     "remaining": hold.remaining, "price": hold.cost,
                     "hint": "This wallet's session cap is reached. Wait for the "
                             "session to expire, or pay for a new session_create "
                             "to open a fresh cap."})
    return body


def public_view(row: dict, receipts: list[dict]) -> dict:
    try:
        remaining = str(Decimal(str(row["max_spend"])) - Decimal(str(row["spent"])))
    except Exception:
        remaining = None
    return {
        "session_id": row["session_id"],
        "payer": row["payer"],
        "network": row["network"],
        "status": row["status"],
        "max_spend": str(row["max_spend"]),
        "spent": str(row["spent"]),
        "remaining": remaining,
        "label": row.get("label"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "enforced_rails": list(ENFORCED_RAILS),
        "receipts": receipts,
    }
