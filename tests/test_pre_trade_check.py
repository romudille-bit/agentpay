"""
test_pre_trade_check.py — the pre_trade_check composite bundle.

Components are stubbed at the real_tool_response layer so verdict logic
is tested deterministically with no network.
"""

import pytest

from gateway.services import tools_runtime


GOOD_OB = {
    "asset": "ETH", "exchange": "Binance", "spread_pct": 0.01,
    "depth": [
        {"notional_usd": 10_000, "slippage_pct": 0.005, "executable": True},
        {"notional_usd": 50_000, "slippage_pct": 0.02, "executable": True},
        {"notional_usd": 250_000, "slippage_pct": 0.09, "executable": True},
    ],
}
CALM_FUNDING = {"rates": [
    {"exchange": "Binance", "funding_rate_pct": 0.01},
    {"exchange": "Bybit", "funding_rate_pct": 0.012},
    {"exchange": "OKX", "funding_rate_pct": 0.008},
]}
CALM_OI = {"long_short_ratio": 1.2, "oi_change_24h_pct": 2.0, "total_oi_usd": 5e9}


def _stub_components(monkeypatch, overrides=None):
    data = {
        "orderbook_depth": GOOD_OB,
        "funding_rates":   CALM_FUNDING,
        "open_interest":   CALM_OI,
    }
    data.update(overrides or {})

    async def fake_rtr(tool_name, params):
        return data[tool_name]

    monkeypatch.setattr(tools_runtime, "real_tool_response", fake_rtr)
    return data


class TestPreTradeCheck:

    @pytest.mark.asyncio
    async def test_calm_market_is_ok(self, monkeypatch):
        _stub_components(monkeypatch)
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "ETH", "size_usd": 10_000, "side": "long"})
        assert r["verdict"] == "ok"
        assert r["factors"]["liquidity"]["level"] == "ok"
        assert r["factors"]["carry"]["level"] == "ok"
        assert r["factors"]["crowding"]["level"] == "ok"
        assert r["factors"]["security"]["level"] == "skipped"
        assert "orderbook_depth" in r["components"]

    @pytest.mark.asyncio
    async def test_thin_book_is_avoid(self, monkeypatch):
        _stub_components(monkeypatch, {"orderbook_depth": {
            "asset": "ETH", "spread_pct": 0.2,
            "depth": [{"notional_usd": 10_000, "slippage_pct": None, "executable": False}],
        }})
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "ETH", "size_usd": 50_000, "side": "long"})
        assert r["factors"]["liquidity"]["level"] == "avoid"
        assert r["verdict"] == "avoid"

    @pytest.mark.asyncio
    async def test_hot_funding_long_is_caution(self, monkeypatch):
        _stub_components(monkeypatch, {"funding_rates": {"rates": [
            {"exchange": "Binance", "funding_rate_pct": 0.07},
            {"exchange": "Bybit", "funding_rate_pct": 0.06},
        ]}})
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "ETH", "size_usd": 10_000, "side": "long"})
        assert r["factors"]["carry"]["level"] == "caution"
        assert r["verdict"] == "caution"

    @pytest.mark.asyncio
    async def test_funding_is_side_aware(self, monkeypatch):
        # Positive funding = longs pay; a SHORT collects it → ok.
        _stub_components(monkeypatch, {"funding_rates": {"rates": [
            {"exchange": "Binance", "funding_rate_pct": 0.07},
        ]}})
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "ETH", "size_usd": 10_000, "side": "short"})
        assert r["factors"]["carry"]["level"] == "ok"
        assert r["verdict"] == "ok"

    @pytest.mark.asyncio
    async def test_crowded_long_with_oi_swing_is_avoid(self, monkeypatch):
        _stub_components(monkeypatch, {"open_interest": {
            "long_short_ratio": 3.4, "oi_change_24h_pct": 28.0, "total_oi_usd": 9e9,
        }})
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "ETH", "size_usd": 10_000, "side": "long"})
        assert r["factors"]["crowding"]["level"] == "avoid"
        assert r["verdict"] == "avoid"

    @pytest.mark.asyncio
    async def test_security_danger_is_avoid(self, monkeypatch):
        data = _stub_components(monkeypatch)
        data["token_security"] = {"risk_level": "danger", "is_honeypot": 1}
        r = await tools_runtime._fetch_pre_trade_check({
            "symbol": "PEPE", "size_usd": 1_000, "side": "long",
            "token_address": "0x" + "a" * 40,
        })
        assert r["factors"]["security"]["level"] == "avoid"
        assert r["verdict"] == "avoid"

    @pytest.mark.asyncio
    async def test_component_failure_degrades_to_caution(self, monkeypatch):
        _stub_components(monkeypatch, {"open_interest": {"error": "upstream down"}})
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "ETH", "size_usd": 10_000, "side": "long"})
        assert r["factors"]["crowding"]["level"] == "unknown"
        assert r["verdict"] == "caution"  # missing data is never 'ok'

    @pytest.mark.asyncio
    async def test_registry_entry_is_paid_trading(self):
        import registry
        t = registry.get_tool("pre_trade_check")
        assert t is not None
        assert t.price_usdc == "0.01"
        assert t.category == "trading"
