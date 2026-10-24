"""
test_bitflow_venue.py — the Bitflow (Stacks DEX) venue inside pre_trade_check.

The quote API is replayed through httpx.MockTransport with a constant-product
style curve, so impact bands, route shape and the degrade paths are checked
without network. The composite's venue selection is tested at the
_fetch_pre_trade_check layer with the Bitflow leg stubbed.
"""

import json

import httpx
import pytest

from gateway.services import bitflow, tools_runtime
from tests.test_pre_trade_check import CALM_FUNDING, CALM_OI, GOOD_OB


STX_TOKEN = {"symbol": "STX", "contract": bitflow.STX, "decimals": 6, "quotable": True}
ZEST_TOKEN = {"symbol": "ZEST", "contract": bitflow._SEED_TOKENS["ZEST"][0],
              "decimals": 6, "quotable": True}


def _curve_client(depth_usd: float, tvl_usd: float = 500_000.0, hops: int = 1,
                  fail_quotes: bool = False, no_route: bool = False):
    """A fake Bitflow: 1 token = $0.25, output shrinks linearly with size/depth."""
    price = 0.25

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/quote/multi"):
            if fail_quotes:
                return httpx.Response(503, text="down")
            if no_route:
                return httpx.Response(200, json={"success": None, "routes": [],
                                                 "error": "Unsupported output token"})
            body = json.loads(req.content)
            amount_in = int(body["amount_in"])
            buying = body["input_token"] == bitflow.USDCX
            usd = amount_in / 1e6 if buying else amount_in / 1e6 * price
            factor = max(0.0, 1 - usd / depth_usd)
            out = (usd / price if buying else usd) * 1e6 * factor
            hop = [{"pool_id": f"dlmm_{i+1}", "bins_traversed": 2} for i in range(hops)]
            route = {"amount_out": str(int(out)), "route_path": ["a", "b"][: hops + 1],
                     "total_hops": hops, "price_impact_bps": 7,
                     "execution_details": {"fee_rate": "0.005", "hop_details": hop}}
            return httpx.Response(200, json={"success": True, "routes": [route],
                                             "best_route_index": 0})
        if "/api/app/v1/pools/" in req.url.path:
            return httpx.Response(200, json={"poolContract": "SM.pool", "tvlUsd": tvl_usd,
                                             "volumeUsd1d": 1234.5, "binStep": 10})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestLiquidityFactor:

    @pytest.mark.asyncio
    async def test_deep_pool_is_ok_with_route_and_tvl(self):
        async with _curve_client(depth_usd=10_000_000) as c:
            f = await bitflow.liquidity_factor(c, STX_TOKEN, "long", 1_000)
        assert f["level"] == "ok"
        assert f["venue"] == "bitflow" and f["quote_asset"] == "USDCx"
        assert f["price_impact_pct"] < bitflow.IMPACT_CAUTION_PCT
        assert f["pools"][0]["tvl_usd"] == 500_000.0
        assert f["hops"] == 1 and f["bins_traversed"] == 2
        assert f["mid_price_usd"] == pytest.approx(0.25, rel=1e-3)

    @pytest.mark.asyncio
    async def test_impact_bands(self):
        # depth 1M: $20k → ~2% impact (caution); $50k → ~5% (avoid).
        async with _curve_client(depth_usd=1_000_000) as c:
            mid = await bitflow.liquidity_factor(c, STX_TOKEN, "long", 20_000)
            big = await bitflow.liquidity_factor(c, STX_TOKEN, "long", 50_000)
        assert mid["level"] == "caution" and 1 <= mid["price_impact_pct"] < 3
        assert big["level"] == "avoid" and big["price_impact_pct"] >= 3

    @pytest.mark.asyncio
    async def test_short_sells_the_token_and_sees_the_same_curve(self):
        async with _curve_client(depth_usd=1_000_000) as c:
            f = await bitflow.liquidity_factor(c, STX_TOKEN, "short", 20_000)
        assert f["level"] == "caution"
        assert f["route"][0] == "a"

    @pytest.mark.asyncio
    async def test_thin_tvl_downgrades_ok_to_caution(self):
        async with _curve_client(depth_usd=10_000_000, tvl_usd=5_000) as c:
            f = await bitflow.liquidity_factor(c, STX_TOKEN, "long", 1_000)
        assert f["level"] == "caution"
        assert "TVL" in f["reason"]

    @pytest.mark.asyncio
    async def test_multi_hop_route_lists_every_pool(self):
        async with _curve_client(depth_usd=10_000_000, hops=3) as c:
            f = await bitflow.liquidity_factor(c, ZEST_TOKEN, "long", 300)
        assert f["hops"] == 3
        assert [p["pool_id"] for p in f["pools"]] == ["dlmm_1", "dlmm_2", "dlmm_3"]

    @pytest.mark.asyncio
    async def test_no_route_is_avoid_never_ok(self):
        async with _curve_client(depth_usd=1e9, no_route=True) as c:
            f = await bitflow.liquidity_factor(c, ZEST_TOKEN, "long", 300)
        assert f["level"] in ("avoid", "unknown")
        assert "route" in f["reason"] or "unavailable" in f["reason"]

    @pytest.mark.asyncio
    async def test_api_down_is_unknown(self):
        async with _curve_client(depth_usd=1e9, fail_quotes=True) as c:
            f = await bitflow.liquidity_factor(c, STX_TOKEN, "long", 1_000)
        assert f["level"] == "unknown"
        assert "unavailable" in f["reason"]

    @pytest.mark.asyncio
    async def test_xyk_only_token_is_unknown_and_says_so(self):
        async with _curve_client(depth_usd=1e9) as c:
            f = await bitflow.liquidity_factor(
                c, {"symbol": "WELSH", "contract": "SP.welsh", "decimals": None,
                    "quotable": False}, "long", 100)
        assert f["level"] == "unknown"
        assert "xyk" in f["reason"]


def _stub(monkeypatch, token, bf_factor=None, cex=None):
    data = {"orderbook_depth": GOOD_OB, "funding_rates": CALM_FUNDING,
            "open_interest": CALM_OI}
    data.update(cex or {})
    seen = {"cex": []}

    async def fake_rtr(tool_name, params):
        seen["cex"].append((tool_name, params))
        return data[tool_name]

    async def fake_resolve(client, symbol):
        return token if token and symbol.upper() == token["symbol"] else None

    async def fake_factor(client, tok, side, size_usd):
        return bf_factor or {"level": "ok", "venue": "bitflow", "price_impact_pct": 0.02,
                             "reason": "~0.02% price impact"}

    monkeypatch.setattr(tools_runtime, "real_tool_response", fake_rtr)
    monkeypatch.setattr(bitflow, "resolve", fake_resolve)
    monkeypatch.setattr(bitflow, "liquidity_factor", fake_factor)
    return seen


class TestVenueSelection:

    @pytest.mark.asyncio
    async def test_eth_path_is_unchanged(self, monkeypatch):
        seen = _stub(monkeypatch, token=None)
        r = await tools_runtime._fetch_pre_trade_check({"symbol": "ETH", "size_usd": 1_000})
        assert r["venue"] == "cex" and "dex_liquidity" not in r["factors"]
        assert r["verdict"] == "ok"
        assert r["not_checked"] == ["security"]  # native n/a, as before

    @pytest.mark.asyncio
    async def test_stx_gets_both_venues(self, monkeypatch):
        _stub(monkeypatch, token=STX_TOKEN)
        r = await tools_runtime._fetch_pre_trade_check({"symbol": "STX", "size_usd": 1_000})
        assert r["venue"] == "cex+bitflow"
        assert r["factors"]["liquidity"]["exchange"] == "Binance"
        assert r["factors"]["dex_liquidity"]["venue"] == "bitflow"
        assert r["factors"]["security"]["level"] == "n/a"
        assert r["verdict"] == "ok"

    @pytest.mark.asyncio
    async def test_sbtc_perp_legs_read_btc(self, monkeypatch):
        sbtc = {"symbol": "SBTC", "contract": bitflow.SBTC, "decimals": 8, "quotable": True}
        seen = _stub(monkeypatch, token=sbtc)
        r = await tools_runtime._fetch_pre_trade_check({"symbol": "sBTC", "size_usd": 5_000})
        assert r["symbol"] == "SBTC" and r["venue"] == "cex+bitflow"
        assert all(p.get("symbol", p.get("asset")) == "BTC" for _, p in seen["cex"])

    @pytest.mark.asyncio
    async def test_dex_only_token_has_na_perp_factors_and_unscreened_security(self, monkeypatch):
        seen = _stub(monkeypatch, token=ZEST_TOKEN)
        r = await tools_runtime._fetch_pre_trade_check({"symbol": "ZEST", "size_usd": 300})
        assert r["venue"] == "bitflow"
        assert seen["cex"] == []                      # no CEX call at all
        assert r["factors"]["liquidity"]["venue"] == "bitflow"
        assert r["factors"]["carry"]["level"] == "n/a"
        assert r["factors"]["crowding"]["level"] == "n/a"
        assert r["factors"]["security"]["level"] == "unknown"
        assert "NOT screened" in r["factors"]["security"]["reason"]
        assert r["verdict"] == "caution"             # unscreened contract caps at caution
        assert set(r["not_checked"]) == {"carry", "crowding", "security"}

    @pytest.mark.asyncio
    async def test_thin_bitflow_pool_drives_the_verdict(self, monkeypatch):
        _stub(monkeypatch, token=STX_TOKEN,
              bf_factor={"level": "avoid", "venue": "bitflow", "price_impact_pct": 4.2,
                         "reason": "~4.2% price impact"})
        r = await tools_runtime._fetch_pre_trade_check({"symbol": "STX", "size_usd": 50_000})
        assert r["factors"]["dex_liquidity"]["level"] == "avoid"
        assert r["verdict"] == "avoid"

    @pytest.mark.asyncio
    async def test_venue_cex_skips_bitflow_entirely(self, monkeypatch):
        _stub(monkeypatch, token=STX_TOKEN)
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "STX", "size_usd": 1_000, "venue": "cex"})
        assert r["venue"] == "cex" and "dex_liquidity" not in r["factors"]

    @pytest.mark.asyncio
    async def test_venue_bitflow_keeps_perp_context_but_dex_liquidity(self, monkeypatch):
        seen = _stub(monkeypatch, token=STX_TOKEN)
        r = await tools_runtime._fetch_pre_trade_check(
            {"symbol": "STX", "size_usd": 1_000, "venue": "bitflow"})
        assert r["venue"] == "bitflow"
        assert r["factors"]["liquidity"]["venue"] == "bitflow"
        assert r["factors"]["carry"]["level"] == "ok"
        assert "orderbook_depth" not in dict(seen["cex"])

    @pytest.mark.asyncio
    async def test_bitflow_crash_is_unknown_not_ok(self, monkeypatch):
        _stub(monkeypatch, token=ZEST_TOKEN)

        async def boom(client, tok, side, size_usd):
            raise RuntimeError("bff timeout")

        monkeypatch.setattr(bitflow, "liquidity_factor", boom)
        r = await tools_runtime._fetch_pre_trade_check({"symbol": "ZEST", "size_usd": 300})
        assert r["factors"]["liquidity"]["level"] == "unknown"
        assert "unavailable" in r["factors"]["liquidity"]["reason"]
        assert r["verdict"] != "ok"
