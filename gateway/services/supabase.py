"""
services/supabase.py — Supabase REST helpers.

Wraps the Supabase REST API in raw httpx — works with the sb_publishable_
key format that the supabase-py SDK can't handle. Today this only logs
completed payments; Tier 2 will expand it into the persisted replay-state
+ payment_logs lifecycle home.
"""

import logging

import httpx

from gateway.config import settings

logger = logging.getLogger(__name__)


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


async def log_payment(
    payment_id: str,
    tool_name: str,
    agent_address: str,
    amount_usdc: str,
    tx_hash: str,
) -> None:
    """Fire-and-forget: log a completed payment to Supabase."""
    if not sb_enabled():
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=sb_headers(),
                json={
                    "payment_id":    payment_id,
                    "tool_name":     tool_name,
                    "agent_address": agent_address,
                    "amount_usdc":   amount_usdc,
                    "tx_hash":       tx_hash,
                    "status":        "completed",
                },
            )
    except Exception as e:
        # Use error-level so failures actually surface in Railway logs.
        # Previously this was warning-level, which buried RLS / auth /
        # network failures under httpx access logs and let writes silently
        # fail for 25 days (March 31 → April 28) before anyone noticed.
        logger.error(
            f"Payment log to Supabase FAILED — paid call NOT recorded "
            f"(payment_id={payment_id}, tool={tool_name}). Error: {e}"
        )
