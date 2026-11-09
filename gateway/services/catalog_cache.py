"""
catalog_cache.py — the whole Bazaar catalog, in memory, refreshed daily.

Bazaar's text search answers one short head per term (1–18 rows), so a
22-term sweep saw ~150 of 33,000 listings and verified_route ranked the
head of a search index, not the market. `/discovery/resources` pages the
full catalog with the same per-listing fields the ranker reads, so the paid
tool now ranks over all of it. The snapshot is held lean in this process
(one uvicorn worker) and refreshed in the background; the live text sweep
stays as a top-up for listings newer than the snapshot. A cold or stale
cache makes `get()` return None and callers fall back to the sweep alone.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import Optional

import httpx

from gateway import radar

logger = logging.getLogger(__name__)

RESOURCES_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
UA = "agentpay-gateway/catalog-refresh (+https://agentpay.tools)"
PAGE = 1000
CONCURRENCY = 6
MAX_PAGES = 100             # a hard stop (100k listings), not a target
FIRST_DELAY_S = 30
CHECK_INTERVAL_S = 24 * 3600
MAX_AGE_H = 72              # older than this ⇒ treated as cold
DESCRIPTION_CHARS = 400

_state: dict = {"cands": None, "fetched_at": None, "total": 0, "pages_failed": 0}


_SCHEMA_KEYS = ("outputSchema", "inputSchema")
_CANON_MAX = 4000  # bytes of JSON; bigger values are kept as-is


class _Canon:
    """One shared instance per identical value. 88% of the catalog is clone
    listings with the same accepts.extra / input_spec / output_keys, so
    sharing them is most of the memory. Shared values are never mutated."""

    def __init__(self) -> None:
        self._by_json: dict[str, object] = {}
        self._str: dict[str, str] = {}

    def s(self, v):
        if isinstance(v, str):
            return self._str.setdefault(v, v)
        return v

    def obj(self, v):
        if isinstance(v, str):
            return self.s(v)
        if not isinstance(v, (dict, list)) or not v:
            return v
        try:
            key = json.dumps(v, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return v
        if len(key) > _CANON_MAX:
            return v
        return self._by_json.setdefault(key, v)


def _lean_accepts(a: dict, canon: _Canon) -> dict:
    """An accepts entry without its schema blobs: has_schema, output_keys and
    input_spec were already derived from them, and a buyer pays without them."""
    return canon.obj({k: canon.obj(v) for k, v in a.items() if k not in _SCHEMA_KEYS})


def _lean(c: dict, canon: _Canon) -> dict:
    """Trim a parsed candidate to what ranking and ready_to_pay use."""
    c = dict(c)
    desc = c.get("description") or ""
    if len(desc) > DESCRIPTION_CHARS:
        desc = desc[:DESCRIPTION_CHARS]
    c["description"] = canon.s(desc)
    c["accepts"] = _lean_accepts(c.get("accepts") or {}, canon)
    rails = c.get("accepts_all") or []
    c["accepts_all"] = ([_lean_accepts(a, canon) for a in rails] if len(rails) > 1
                        else ([c["accepts"]] if c["accepts"] else []))
    for k in ("pay_to", "network", "network_caip2", "name", "last_called"):
        c[k] = canon.s(c.get(k))
    for k in ("tags", "networks_all", "output_keys", "input_spec"):
        c[k] = canon.obj(c.get(k))
    return c


def _parse_page(items: list[dict], seen: set[str], canon: _Canon) -> list[dict]:
    """Parse one page straight to lean candidates so raw items never pile up."""
    out = []
    for c in radar.parse_resources({"resources": items}):
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        out.append(_lean(c, canon))
    return out


async def _page(client: httpx.AsyncClient, offset: int) -> dict:
    for attempt in range(3):
        try:
            r = await client.get(RESOURCES_URL, params={"limit": PAGE, "offset": offset},
                                 headers={"User-Agent": UA, "Accept": "application/json"})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    return {}


async def fetch_catalog(client: Optional[httpx.AsyncClient] = None) -> tuple[list[dict], int, int]:
    """Returns (lean candidates, total the API reports, pages that failed)."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=60.0)
    try:
        first = await _page(client, 0)
        total = int((first.get("pagination") or {}).get("total") or len(first.get("items") or []))
        seen: set[str] = set()
        canon = _Canon()
        cands = _parse_page(first.get("items") or [], seen, canon)
        offsets = list(range(PAGE, min(total, PAGE * MAX_PAGES), PAGE))
        sem = asyncio.Semaphore(CONCURRENCY)
        failed = 0

        async def one(off: int) -> list[dict]:
            nonlocal failed
            async with sem:
                try:
                    return (await _page(client, off)).get("items") or []
                except Exception as e:
                    failed += 1
                    logger.warning(f"[CATALOG] page at offset {off} failed: {e}")
                    return []

        # Pages are parsed as they complete; the raw JSON of a page is dropped
        # before the next one is held.
        for coro in asyncio.as_completed([one(o) for o in offsets]):
            cands.extend(_parse_page(await coro, seen, canon))
        return cands, total, failed
    finally:
        if own:
            await client.aclose()


async def refresh() -> int:
    """Replace the snapshot. Keeps the old one when the new pull lost >10% of pages."""
    cands, total, failed = await fetch_catalog()
    if not cands:
        logger.warning("[CATALOG] refresh returned no listings — keeping the previous snapshot")
        return 0
    if failed and failed * PAGE > total // 10 and _state["cands"]:
        logger.warning(f"[CATALOG] {failed} pages failed — keeping the previous snapshot")
        return 0
    _state.update(cands=cands, fetched_at=datetime.now(timezone.utc),
                  total=total, pages_failed=failed)
    logger.info(f"[CATALOG] cached {len(cands)} listings ({total} reported, {failed} pages failed)")
    return len(cands)


def age_hours() -> Optional[float]:
    at = _state["fetched_at"]
    return None if at is None else (datetime.now(timezone.utc) - at).total_seconds() / 3600


def get() -> Optional[list[dict]]:
    """The cached candidates, or None when cold or stale (callers sweep instead)."""
    age = age_hours()
    if _state["cands"] is None or age is None or age > MAX_AGE_H:
        return None
    return _state["cands"]


def status() -> dict:
    age = age_hours()
    return {
        "listings": len(_state["cands"] or []),
        "reported_total": _state["total"],
        "age_hours": round(age, 1) if age is not None else None,
        "fresh": get() is not None,
    }


async def refresh_loop() -> None:
    """Warm shortly after boot, then once a day. Register in main.py's lifespan."""
    await asyncio.sleep(FIRST_DELAY_S + random.uniform(0, 30))
    while True:
        try:
            await refresh()
        except Exception as e:  # pragma: no cover — the loop must survive anything
            logger.error(f"catalog refresh loop error: {e}")
        await asyncio.sleep(CHECK_INTERVAL_S)
