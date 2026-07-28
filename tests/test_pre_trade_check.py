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
        # AGE-84: ETH is a native asset — the security factor is explicitly
        # n/a (no contract exists), never a silent "skipped".
        assert r["factors"]["security"]["level"] == "n/a"
        assert "native" in r["factors"]["security"]["reason"]
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


class TestSecurityLegResolution:
    """AGE-84: the security leg was skipped on 100% of symbol-only calls —
    by construction, not by chance — and 'skipped' then vanished from the
    verdict. Every flagship 'ok' in July was 3-of-4 factors with contract
    screening silently off, on a public ledger. Now every call resolves to
    exactly one of: screened / n/a (native) / unknown (counts)."""

    @staticmethod
    def _stub_with_capture(monkeypatch, overrides=None):
        data = {
            "orderbook_depth": GOOD_OB,
            "funding_rates":   CALM_FUNDING,
            "open_interest":   CALM_OI,
            "token_security":  {"risk_level": "safe"},
        }
        data.update(overrides or {})
        calls = {}

        async def fake_rtr(tool_name, params):
            calls[tool_name] = params
            return data[tool_name]

        monkeypatch.setattr(tools_runtime, "real_tool_response", fake_rtr)
        return calls

    @pytest.mark.asyncio
    async def test_known_erc20_is_auto_screened(self, monkeypatch):
        # LINK is exactly the case the issue names: an ERC-20 the flagship
        # screened for a $25k long with the one rug-catching factor off.
        calls = self._stub_with_capture(monkeypatch)
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "LINK", "size_usd": 25_000, "side": "long"})
        assert calls["token_security"]["contract_address"] == \
            "0x514910771af9ca656af840dff83e8264ecf986ca"
        assert calls["token_security"]["chain"] == "ethereum"
        assert r["factors"]["security"]["level"] == "ok"
        assert r["verdict"] == "ok"

    @pytest.mark.asyncio
    async def test_l2_token_is_screened_on_its_home_chain(self, monkeypatch):
        # ARB trades on Arbitrum — screening the L1 bridge copy would be
        # screening the wrong contract.
        calls = self._stub_with_capture(monkeypatch)
        await tools_runtime._fetch_pre_trade_check(
            {"symbol": "ARB", "size_usd": 25_000, "side": "long"})
        assert calls["token_security"]["chain"] == "arbitrum"

    @pytest.mark.asyncio
    async def test_dangerous_known_token_flips_the_verdict(self, monkeypatch):
        # The whole point of AGE-84: a honeypot alt must NOT get "ok" just
        # because the caller sent a symbol instead of an address.
        self._stub_with_capture(monkeypatch, {
            "token_security": {"risk_level": "danger", "is_honeypot": 1}})
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "UNI", "size_usd": 25_000, "side": "long"})
        assert r["factors"]["security"]["level"] == "avoid"
        assert r["verdict"] == "avoid"

    @pytest.mark.asyncio
    async def test_native_asset_is_na_and_excluded(self, monkeypatch):
        self._stub_with_capture(monkeypatch)
        for sym in ("BTC", "DOGE", "ADA", "SOL"):
            r = await tools_runtime._fetch_pre_trade_check(
                {"symbol": sym, "size_usd": 25_000, "side": "long"})
            assert r["factors"]["security"]["level"] == "n/a", sym
            # n/a is excluded: a clean market still reads ok.
            assert r["verdict"] == "ok", sym

        # ...but the exclusion never resurrects "skipped"-style blindness:
        # the factor is present and self-explanatory in the response.
        assert "no token contract" in r["factors"]["security"]["reason"]

    @pytest.mark.asyncio
    async def test_native_stub_never_calls_goplus(self, monkeypatch):
        calls = self._stub_with_capture(monkeypatch)
        await tools_runtime._fetch_pre_trade_check(
            {"symbol": "BTC", "size_usd": 25_000, "side": "long"})
        assert "token_security" not in calls

    @pytest.mark.asyncio
    async def test_unknown_symbol_counts_toward_the_verdict(self, monkeypatch):
        # An unrecognised token has a contract SOMEWHERE — the case screening
        # exists for. "ok" must be unreachable while it's unscreened.
        self._stub_with_capture(monkeypatch)
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "FARTCOIN", "size_usd": 25_000, "side": "long"})
        assert r["factors"]["security"]["level"] == "unknown"
        assert "token_address" in r["factors"]["security"]["reason"]
        assert r["verdict"] == "caution"       # clean market, but unscreened

    @pytest.mark.asyncio
    async def test_explicit_address_beats_every_classification(self, monkeypatch):
        # A caller who KNOWS the contract gets it screened even for a symbol
        # we'd otherwise call native (e.g. a suspicious wrapped/fake "ETH").
        calls = self._stub_with_capture(monkeypatch, {
            "token_security": {"risk_level": "danger"}})
        r = await tools_runtime._fetch_pre_trade_check({
            "symbol": "ETH", "size_usd": 25_000, "side": "long",
            "token_address": "0x" + "b" * 40, "chain": "bsc"})
        assert calls["token_security"]["contract_address"] == "0x" + "b" * 40
        assert calls["token_security"]["chain"] == "bsc"
        assert r["verdict"] == "avoid"

    @pytest.mark.asyncio
    async def test_goplus_failure_on_resolved_token_is_unknown_not_ok(self, monkeypatch):
        # Asymmetry the issue called out, preserved on purpose: a screen that
        # RAN and errored lowers the verdict rather than vanishing.
        self._stub_with_capture(monkeypatch, {
            "token_security": {"error": "GoPlus timeout"}})
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "LINK", "size_usd": 25_000, "side": "long"})
        assert r["factors"]["security"]["level"] == "unknown"
        assert r["verdict"] == "caution"

    @pytest.mark.asyncio
    async def test_skipped_is_extinct(self, monkeypatch):
        # Every rotation symbol the flagship screens must resolve to a real
        # security disposition — the silent 4th state is gone.
        self._stub_with_capture(monkeypatch)
        for sym in ("BTC", "ETH", "SOL", "AVAX", "ARB", "OP",
                    "LINK", "UNI", "DOGE", "ADA"):
            r = await tools_runtime._fetch_pre_trade_check(
                {"symbol": sym, "size_usd": 25_000, "side": "long"})
            assert r["factors"]["security"]["level"] != "skipped", sym
