"""
probe_rollup.py — aggregate 402/probe telemetry without per-event DB writes.

Context (disk-IO fix, 2026-08-04): the Supabase Disk IO budget was being
drained by write churn — every 402 issued (including every crawler GET probe
and scanner POST) cost 2-3 PostgREST round-trips (pending_challenges INSERT,
payment_logs pending INSERT, later sweep UPDATE). 99.5% of payment_logs rows
were 'abandoned' bot probes. Those per-event writes are gone; THIS module is
what preserves the market signal they carried.

Every 402 issuance is counted in an in-memory Counter keyed
(utc_day, tool_name, user_agent, kind) and flushed as a batch INSERT into
`payment_logs_daily_rollup` every ROLLUP_FLUSH_INTERVAL_SECONDS (hourly since
disk-IO fix #3) — one write per window regardless of probe volume, and one
row per live key per window. Rows are ADDITIVE events: consumers
SUM(n) GROUP BY day/tool/user_agent, so no server-side upsert/increment is
needed. New crawlers, UA changes, and volume trends all remain visible in
the rollup (the "who monitors AgentPay" feed for the weekly market review).

kind (stored in the rollup's `state` column, disjoint from payment_logs
lifecycle states):
  probe_get — GET discovery probe (crawlers validating our 402s)
  free_402  — POST 402 on a $0 tool (no pending row is written for these)
  paid_402  — POST 402 on a priced tool (pending row still written; counted
              here too so rollup totals are complete — dedupe by kind when
              joining against payment_logs)

Durability: counts held in memory are lost on a crash mid-window (≤ one
flush interval of telemetry). Acceptable for market telemetry; payments are
not tracked here.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import httpx

from gateway.config import settings
from gateway.services import supabase as sb

logger = logging.getLogger(__name__)

# Loop tick. provider_map (AGE-138) still flushes every tick so verified_route
# discoveries land within minutes; the ROLLUP itself flushes once an hour.
FLUSH_INTERVAL_SECONDS = 300

# Disk-IO fix #3 (2026-09-01): rows are additive EVENTS, so every flush
# appends one row per live (day, tool, UA, kind) key. At a 5-min cadence
# with ~30 keys alive per window that was ~8,400 rows/day (139,848 rows by
# 09-01; 3rd "Disk IO budget" email on 08-29) — the rollup had become the
# write churn it was built to remove. Hourly: ~700 rows/day. A crash now
# loses ≤1h of probe telemetry instead of ≤5min; payments are never
# tracked here, so that is the whole cost. Consumers still SUM(n) GROUP BY.
ROLLUP_FLUSH_INTERVAL_SECONDS = 3600

# Bound memory: (day × tool × UA × kind) keys. 5k keys ≈ a very hostile UA
# rotation; beyond that, new keys collapse into the '(overflow)' UA bucket
# so totals stay accurate even under a UA-randomizing scanner.
_MAX_KEYS = 5000
_OVERFLOW_UA = "(overflow)"

_counts: Counter = Counter()


def record_402(tool_name: str, user_agent: Optional[str], kind: str) -> None:
    """Count one 402 issuance. Synchronous, in-memory, never raises."""
    try:
        day = datetime.now(timezone.utc).date().isoformat()
        ua = (user_agent or "(none)")[:160]
        key = (day, tool_name, ua, kind)
        if key not in _counts and len(_counts) >= _MAX_KEYS:
            key = (day, tool_name, _OVERFLOW_UA, kind)
        _counts[key] += 1
    except Exception:  # pragma: no cover — telemetry must never break a 402
        pass


async def flush() -> int:
    """Flush accumulated counts as ONE batch INSERT. Returns rows written.

    On failure the snapshot is merged back so the next flush retries —
    counts are never silently dropped (until process exit)."""
    if not _counts or not sb.sb_enabled():
        return 0
    snapshot = dict(_counts)
    _counts.clear()
    rows = [
        {"day": day, "tool_name": tool, "user_agent": ua, "state": kind,
         "network": "", "n": n}
        for (day, tool, ua, kind), n in snapshot.items()
    ]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs_daily_rollup",
                headers={**sb.sb_headers(), "Prefer": "return=minimal"},
                json=rows,
            )
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")
        return len(rows)
    except Exception as e:
        logger.warning(
            f"probe rollup flush failed ({e}) — re-queuing {len(rows)} keys"
        )
        for key, n in snapshot.items():
            _counts[key] += n
        return 0


def _rollup_due(last_flush: float, now: float) -> bool:
    """Pure: has ROLLUP_FLUSH_INTERVAL_SECONDS elapsed since the last
    rollup flush? (Failed flushes re-queue and retry on the next due tick.)"""
    return now - last_flush >= ROLLUP_FLUSH_INTERVAL_SECONDS


async def flush_loop() -> None:
    """Background flusher — register in main.py's lifespan.

    Ticks every FLUSH_INTERVAL_SECONDS; the rollup INSERT runs only when
    ROLLUP_FLUSH_INTERVAL_SECONDS has elapsed, provider_map every tick."""
    import time as _time
    last_rollup_flush = _time.monotonic()
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        now = _time.monotonic()
        if _rollup_due(last_rollup_flush, now):
            last_rollup_flush = now
            try:
                n = await flush()
                if n:
                    logger.info(f"[ROLLUP] flushed {n} probe-count rows")
            except Exception as e:  # pragma: no cover
                logger.error(f"probe rollup loop error: {e}")
        # AGE-138: provider rows discovered by verified_route since the last
        # window — same batch vehicle, same one-write-per-window rule.
        try:
            from gateway.services import provider_map
            await provider_map.flush()
        except Exception as e:  # pragma: no cover
            logger.error(f"provider_map flush error: {e}")
