"""
test_cache.py — TTL response cache in services/tools_runtime.

Pins: one upstream call for two identical requests inside the TTL, the
`cached` flag on hits, param-scoped keys, errors never cached, and
uncached tools always hitting upstream.
"""

import pytest

from gateway.services import tools_runtime
from gateway.services.cache import CACHE_TTL


@pytest.fixture
def counted_fetch(monkeypatch):
    """Replace _fetch_token_price with a call counter."""
    calls = {"n": 0}

    async def fake_fetch(client, params):
        calls["n"] += 1
        return {"symbol": params.get("symbol", "?"), "price_usd": 100.0}

    monkeypatch.setattr(tools_runtime, "_fetch_token_price", fake_fetch)
    return calls


class TestResponseCache:

    @pytest.mark.asyncio
    async def test_second_identical_call_is_cached(self, counted_fetch):
        r1 = await tools_runtime.real_tool_response("token_price", {"symbol": "ETH"})
        r2 = await tools_runtime.real_tool_response("token_price", {"symbol": "ETH"})
        assert counted_fetch["n"] == 1
        assert "cached" not in r1          # live read carries no flag
        assert r2.get("cached") is True    # hit is flagged
        assert r2["price_usd"] == r1["price_usd"]

    @pytest.mark.asyncio
    async def test_different_params_miss(self, counted_fetch):
        await tools_runtime.real_tool_response("token_price", {"symbol": "ETH"})
        await tools_runtime.real_tool_response("token_price", {"symbol": "BTC"})
        assert counted_fetch["n"] == 2

    @pytest.mark.asyncio
    async def test_errors_are_not_cached(self, monkeypatch):
        calls = {"n": 0}

        async def failing_fetch(client, params):
            calls["n"] += 1
            return {"error": "upstream down"}

        monkeypatch.setattr(tools_runtime, "_fetch_token_price", failing_fetch)
        await tools_runtime.real_tool_response("token_price", {"symbol": "ETH"})
        await tools_runtime.real_tool_response("token_price", {"symbol": "ETH"})
        assert calls["n"] == 2  # error responses always retry upstream

    @pytest.mark.asyncio
    async def test_cached_flag_does_not_leak_into_store(self, counted_fetch):
        await tools_runtime.real_tool_response("token_price", {"symbol": "ETH"})
        r2 = await tools_runtime.real_tool_response("token_price", {"symbol": "ETH"})
        r3 = await tools_runtime.real_tool_response("token_price", {"symbol": "ETH"})
        assert r2.get("cached") is True and r3.get("cached") is True
        # The stored entry itself must stay unflagged (hits return copies)
        from gateway.services.cache import _cache
        stored = next(v for _, v in _cache.values())
        assert "cached" not in stored

    def test_newer_tools_have_ttls(self):
        for tool in ("token_security", "yield_scanner", "funding_rates",
                     "open_interest", "orderbook_depth", "crypto_news",
                     "whale_activity"):
            assert tool in CACHE_TTL, f"{tool} missing from CACHE_TTL"
        # session_create is stateful and must never be cached
        assert "session_create" not in CACHE_TTL
