"""
services/cache.py — In-memory response cache for tool calls.

Per-tool TTLs in CACHE_TTL keep popular reads cheap without serving stale
prices. Cache misses fall through to the live API. Tools not in CACHE_TTL
are never cached. State dies with the worker — Tier 2 will move this to
Supabase or Redis along with the replay tables.

AGE-70: the cache is BOUNDED. Keys include caller-supplied params (e.g.
verified_route by need+budget+chain), so without a cap an outsider cycling
unique params grew the dict without limit → OOM/restart (which also wiped
replay state). Expired entries are evicted on access; a full cache evicts
FIFO on insert so memory is bounded at _MAXSIZE regardless of input. (A
single-ttl cachetools.TTLCache doesn't fit our per-key TTLs, hence the small
custom bound.)
"""

import time as _time

# key → (expires_at_monotonic, data). Insertion order is preserved by dict,
# so the FIFO victim is always next(iter(_cache)).
_cache: dict[str, tuple[float, dict]] = {}

# Hard ceiling on distinct cached keys. ~5k small JSON payloads is a few MB —
# comfortably bounded while covering every real tool×params combination.
_MAXSIZE = 5000

CACHE_TTL: dict[str, int] = {
    "token_price":        60,   # 60 seconds
    "gas_tracker":        30,   # 30 seconds
    "fear_greed_index":   300,  # 5 minutes
    "defi_tvl":           300,  # 5 minutes
    "token_market_data":  120,  # 2 minutes
    "dex_liquidity":      120,  # legacy alias — same TTL
    "url_reader":         300,  # 5 minutes — page content rarely changes mid-session
    "web_search":         120,  # 2 minutes — search results can shift
    "market_snapshot":    60,   # 60 seconds — macro + crypto ticks frequently
    "token_security":     3600, # 1 hour — contract scan results change rarely
    "yield_scanner":      300,  # 5 minutes — pool APYs move slowly
    "funding_rates":      60,   # funding updates hourly; 60s is conservative
    "open_interest":      60,
    "orderbook_depth":    15,   # shortest — depth is the most time-sensitive read
    "crypto_news":        120,
    "whale_activity":     60,
    "verified_route":     120,  # multi-query catalog sweep is expensive; cache by need+budget+chain
}


def cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    if _time.monotonic() < entry[0]:
        return entry[1]
    # Expired — evict on access so one-shot keys don't linger.
    _cache.pop(key, None)
    return None


def _evict_one() -> None:
    """Make room for one entry. Prefer dropping an expired entry (bounded
    peek at the oldest few, cheap under FIFO churn); else drop the
    oldest-inserted. O(1) amortised."""
    now = _time.monotonic()
    # Peek a handful of the oldest entries for an already-expired victim.
    for k in list(_cache.keys())[:8]:
        if _cache[k][0] <= now:
            _cache.pop(k, None)
            return
    # None expired among the oldest — FIFO-evict the oldest-inserted.
    oldest = next(iter(_cache), None)
    if oldest is not None:
        _cache.pop(oldest, None)


def cache_set(key: str, value: dict, ttl: int) -> None:
    if key not in _cache and len(_cache) >= _MAXSIZE:
        _evict_one()
    _cache[key] = (_time.monotonic() + ttl, value)
