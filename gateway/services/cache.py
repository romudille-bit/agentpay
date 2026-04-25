"""
services/cache.py — In-memory response cache for tool calls.

Per-tool TTLs in CACHE_TTL keep popular reads cheap without serving stale
prices. Cache misses fall through to the live API. Tools not in CACHE_TTL
are never cached. State dies with the worker — Tier 2 will move this to
Supabase or Redis along with the replay tables.
"""

import time as _time

# key → (expires_at_monotonic, data)
_cache: dict[str, tuple[float, dict]] = {}

CACHE_TTL: dict[str, int] = {
    "token_price":        60,   # 60 seconds
    "gas_tracker":        30,   # 30 seconds
    "fear_greed_index":   300,  # 5 minutes
    "defi_tvl":           300,  # 5 minutes
    "token_market_data":  120,  # 2 minutes
    "dex_liquidity":      120,  # legacy alias — same TTL
}


def cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry and _time.monotonic() < entry[0]:
        return entry[1]
    return None


def cache_set(key: str, value: dict, ttl: int) -> None:
    _cache[key] = (_time.monotonic() + ttl, value)
