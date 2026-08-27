"""
provider_map.py — hold provider rows discovered on the CUSTOMER path until the
next batch flush (AGE-138).

verified_route sweeps ~20 Bazaar queries per paid call and scores ~200
candidates; every one of them resolves payTo → provider (host, URLs, need).
Persisting that from the request would put a Supabase write on the live path
— the exact thing the 08-20 disk-IO fix removed. So rows are merged into a
bounded in-memory dict here and written by probe_rollup's flush loop (one
upsert per window, however many calls came in). The prober's own sweep posts
its rows directly to /v1/prober/run, which upserts them there (once per sweep).

Bounded: at most _MAX_PENDING (pay_to, network) keys; beyond that, new
providers are dropped until the next flush (they'll be seen again on the next
sweep — losing a holding-window entry is not losing data).
"""
from __future__ import annotations

import logging

from gateway.services import supabase as sb

logger = logging.getLogger(__name__)

_MAX_PENDING = 2000
_pending: dict[tuple[str, str], dict] = {}


def remember(rows: list[dict]) -> int:
    """Merge sweep-derived provider rows into the holding dict. Never raises."""
    added = 0
    try:
        for r in rows or []:
            key = (str(r.get("pay_to") or "").lower(), str(r.get("network") or ""))
            if not key[0] or not key[1]:
                continue
            if key not in _pending and len(_pending) >= _MAX_PENDING:
                continue
            cur = _pending.get(key)
            if cur is None:
                _pending[key] = r
                added += 1
            else:
                for u in r.get("resource_urls") or []:
                    if u not in cur["resource_urls"] and len(cur["resource_urls"]) < 40:
                        cur["resource_urls"].append(u)
                for k, v in (r.get("categories") or {}).items():
                    cur["categories"][k] = cur["categories"].get(k, 0) + v
                for src in r.get("sources") or []:
                    if src not in cur["sources"]:
                        cur["sources"].append(src)
                cur["listings"] = len(cur["resource_urls"])
                ce, re_ = cur.setdefault("evidence", {}), r.get("evidence") or {}
                ce["payers30d"] = max(ce.get("payers30d", 0), re_.get("payers30d", 0))
                ce["calls30d"] = max(ce.get("calls30d", 0), re_.get("calls30d", 0))
    except Exception:  # pragma: no cover — telemetry must never break a route
        pass
    return added


def pending_count() -> int:
    return len(_pending)


async def flush() -> int:
    """ONE upsert of everything held. Returns rows written; on failure the
    snapshot is put back so the next flush retries."""
    if not _pending or not sb.sb_enabled():
        return 0
    snapshot = dict(_pending)
    _pending.clear()
    n = await sb.upsert_provider_map(list(snapshot.values()))
    if n == 0:
        for k, v in snapshot.items():
            _pending.setdefault(k, v)
        return 0
    logger.info(f"[PROVIDER_MAP] flushed {n} provider rows")
    return n
