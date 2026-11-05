"""
test_funding_spread.py — the funding_spread paid tool.

Every venue is replayed through httpx.MockTransport with recorded shapes, so
normalisation, spread maths, the 7-day percentile and the degrade paths are
checked without network. The route contract (paid 200 + receipt header) is
covered by the generic tools-route tests.
"""
import json

import httpx
import pytest

from gateway.services import funding_spread as fs

NOW = 1_793_882_400_000                      # 2026-11-05 12:40 UTC
H8 = fs.EIGHT_H_MS


def _hist8(rate_pct: float, n: int = 21, step=H8, now=NOW):
    """n settlements at the given per-interval rate (percent), newest first."""
    return [(now - i * step, rate_pct) for i in range(1, n + 1)]


class FakeVenues:
    """Per-venue current rates (percent per interval) and optional history (as (ms, pct))."""

    def __init__(self, rates: dict, history: dict | None = None,
                 down: set | None = None, status: dict | None = None):
        self.rates, self.history = rates, history or {}
        self.down, self.status = down or set(), status or {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        h, p = req.url.host, req.url.path
        v = {"fapi.binance.com": "binance", "api.bybit.com": "bybit", "www.okx.com": "okx",
             "api.gateio.ws": "gate", "api.bitget.com": "bitget",
             "api.hyperliquid.xyz": "hyperliquid", "indexer.dydx.trade": "dydx",
             "contract.mexc.com": "mexc", "api-futures.kucoin.com": "kucoin"}[h]
        if v in self.down:
            return httpx.Response(self.status.get(v, 503), text="down")
        r = self.rates.get(v)
        if r is None:
            return httpx.Response(400, json={"error": "no such contract"})
        rr = r / 100                                  # venues speak in fractions
        hist = [(t, x / 100) for t, x in self.history.get(v, [])]

        if v == "binance":
            if p.endswith("/premiumIndex"):
                return httpx.Response(200, json={"symbol": "BTCUSDT", "lastFundingRate": str(rr),
                                                 "nextFundingTime": NOW + H8})
            if p.endswith("/fundingInfo"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"fundingTime": t, "fundingRate": str(x)} for t, x in hist])
        if v == "bybit":
            if p.endswith("/tickers"):
                return httpx.Response(200, json={"result": {"list": [
                    {"symbol": "BTCUSDT", "fundingRate": str(rr), "nextFundingTime": str(NOW + H8)}]}})
            if p.endswith("/instruments-info"):
                return httpx.Response(200, json={"result": {"list": [{"fundingInterval": "480"}]}})
            return httpx.Response(200, json={"result": {"list": [
                {"fundingRateTimestamp": str(t), "fundingRate": str(x)} for t, x in hist]}})
        if v == "okx":
            if p.endswith("/funding-rate"):
                return httpx.Response(200, json={"data": [{"fundingRate": str(rr), "fundingTime": str(NOW),
                                                           "nextFundingTime": str(NOW + H8)}]})
            return httpx.Response(200, json={"data": [
                {"fundingTime": str(t), "realizedRate": str(x)} for t, x in hist]})
        if v == "gate":
            if "/contracts/" in p:
                return httpx.Response(200, json={"funding_rate": str(rr), "funding_interval": 28800,
                                                 "funding_next_apply": (NOW + H8) // 1000})
            return httpx.Response(200, json=[{"t": t // 1000, "r": str(x)} for t, x in hist])
        if v == "bitget":
            if p.endswith("/current-fund-rate"):
                return httpx.Response(200, json={"data": [{"fundingRate": str(rr), "fundingRateInterval": "8",
                                                           "nextUpdate": str(NOW + H8)}]})
            return httpx.Response(200, json={"data": [
                {"fundingTime": str(t), "fundingRate": str(x)} for t, x in hist]})
        if v == "hyperliquid":
            body = json.loads(req.content)
            if body["type"] == "metaAndAssetCtxs":
                return httpx.Response(200, json=[{"universe": [{"name": "ETH"}, {"name": "BTC"}]},
                                                 [{"funding": "0"}, {"funding": str(rr)}]])
            return httpx.Response(200, json=[{"time": t, "fundingRate": str(x)} for t, x in hist])
        if v == "dydx":
            if p.endswith("/perpetualMarkets"):
                return httpx.Response(200, json={"markets": {"BTC-USD": {"nextFundingRate": str(rr)}}})
            from datetime import datetime, timezone
            rows = [{"effectiveAt": datetime.fromtimestamp(t / 1000, tz=timezone.utc)
                     .isoformat().replace("+00:00", "Z"), "rate": str(x)} for t, x in hist]
            return httpx.Response(200, json={"historicalFunding": rows})
        if v == "mexc":
            if p.endswith("/history"):
                return httpx.Response(200, json={"data": {"resultList": [
                    {"settleTime": t, "fundingRate": x} for t, x in hist]}})
            return httpx.Response(200, json={"data": {"fundingRate": rr, "collectCycle": 8,
                                                      "nextSettleTime": NOW + H8}})
        if v == "kucoin":
            if p.endswith("/current"):
                return httpx.Response(200, json={"data": {"value": rr, "granularity": 28800000,
                                                          "fundingTime": NOW + H8}})
            return httpx.Response(200, json={"data": [{"timepoint": t, "fundingRate": x} for t, x in hist]})
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


EIGHT_H_VENUES = {"binance": 0.01, "bybit": 0.012, "okx": 0.01, "gate": 0.008,
                  "bitget": 0.03, "mexc": 0.01, "kucoin": 0.01}


@pytest.mark.asyncio
async def test_hourly_venues_are_normalised_to_8h():
    # hyperliquid and dYdX quote per hour; 0.00125%/h == 0.01%/8h
    fake = FakeVenues({**EIGHT_H_VENUES, "hyperliquid": 0.00125, "dydx": 0.0025})
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC"}, now_ms=NOW)
    by = {v["venue"]: v for v in r["venues"]}
    assert by["hyperliquid"]["interval_hours"] == 1.0
    assert by["hyperliquid"]["rate_8h_pct"] == pytest.approx(0.01)
    assert by["hyperliquid"]["raw_rate_pct"] == pytest.approx(0.00125)
    assert by["dydx"]["rate_8h_pct"] == pytest.approx(0.02)
    assert by["bybit"]["interval_hours"] == 8.0              # 480 min from instruments-info
    assert by["okx"]["rate_annualized_pct"] == pytest.approx(0.01 * 3 * 365, rel=1e-3)
    assert r["venues_unavailable"] == {}


@pytest.mark.asyncio
async def test_spread_pair_and_read():
    fake = FakeVenues(EIGHT_H_VENUES)
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC"}, now_ms=NOW)
    s = r["spread"]
    assert s["cheapest"] == "gate" and s["dearest"] == "bitget"
    assert s["pct_8h"] == pytest.approx(0.022)
    assert s["bps_8h"] == pytest.approx(2.2)
    assert s["median_8h_pct"] == pytest.approx(0.01)
    assert s["max_deviation_from_median_pct"] == pytest.approx(0.02)
    assert [v["venue"] for v in r["venues"]][0] == "gate"     # sorted cheapest first
    assert r["read"].startswith("BTC funding 0.0300%/8h on bitget vs 0.0080% on gate — 2.2 bps/8h")
    assert "(7 of 9 venues answered)" in r["read"]
    assert set(r["venues_unavailable"]) == {"hyperliquid", "dydx"}


@pytest.mark.asyncio
async def test_history_percentile_from_8h_buckets():
    # 21 past settlements: okx steady at 0.01, bitget steady at 0.02 → every past
    # bucket's spread is 0.01. Current spread 0.022 beats all of them → 100th pct.
    fake = FakeVenues(EIGHT_H_VENUES,
                      history={"okx": _hist8(0.01), "bitget": _hist8(0.02)})
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC"}, now_ms=NOW)
    h = r["history"]
    assert h["days"] == 7 and h["buckets"] == 21
    assert h["venues_with_history"] == ["bitget", "okx"]
    assert h["spread_pct_8h_p50"] == pytest.approx(0.01)
    assert h["current_percentile"] == 100
    assert "100th percentile of the last 7 days" in r["read"]


@pytest.mark.asyncio
async def test_history_mixes_hourly_and_8h_venues_in_the_same_bucket():
    # dYdX settles hourly at 0.00375%/h (= 0.03%/8h); okx at 0.01%/8h → bucket spread 0.02
    fake = FakeVenues({**EIGHT_H_VENUES, "dydx": 0.00375},
                      history={"okx": _hist8(0.01), "dydx": _hist8(0.00375, n=168, step=3_600_000)})
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC"}, now_ms=NOW)
    assert r["history"]["spread_pct_8h_p50"] == pytest.approx(0.02, abs=1e-6)
    assert r["history"]["spread_pct_8h_max"] == pytest.approx(0.02, abs=1e-6)


@pytest.mark.asyncio
async def test_single_venue_history_yields_no_percentile():
    fake = FakeVenues(EIGHT_H_VENUES, history={"okx": _hist8(0.01)})
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC"}, now_ms=NOW)
    assert r["history"]["buckets"] == 0
    assert r["history"]["current_percentile"] is None
    assert "percentile" not in r["read"]


@pytest.mark.asyncio
async def test_carry_estimate_at_notional():
    fake = FakeVenues(EIGHT_H_VENUES)
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC", "notional_usd": 10000}, now_ms=NOW)
    ce = r["carry_estimate"]
    assert ce["long_on"] == "gate" and ce["short_on"] == "bitget"
    assert ce["per_8h_usd"] == pytest.approx(2.2)
    assert ce["per_day_usd"] == pytest.approx(6.6)
    assert ce["per_30d_usd"] == pytest.approx(198.0)
    assert ce["taker_fees_round_trip_usd"] == pytest.approx(20.0)   # 0.05% × 4 legs
    assert ce["breakeven_hours"] == pytest.approx(20 / 2.2 * 8, rel=1e-3)


@pytest.mark.asyncio
async def test_no_notional_no_carry():
    fake = FakeVenues(EIGHT_H_VENUES)
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC"}, now_ms=NOW)
    assert r["carry_estimate"] is None


@pytest.mark.asyncio
async def test_geo_blocked_venues_are_named_not_fatal():
    fake = FakeVenues(EIGHT_H_VENUES, down={"binance", "bybit"}, status={"binance": 451, "bybit": 403})
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC"}, now_ms=NOW)
    assert r["venues_unavailable"]["binance"] == "geo-blocked (451)"
    assert r["venues_unavailable"]["bybit"] == "geo-blocked (403)"
    assert len(r["venues"]) == 5


@pytest.mark.asyncio
async def test_fewer_than_two_venues_is_an_error_not_a_spread():
    fake = FakeVenues({"okx": 0.01})
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": "BTC"}, now_ms=NOW)
    assert "error" in r and "needs two" in r["error"]
    assert r["venues_unavailable"]["gate"] == "http 400"


@pytest.mark.asyncio
async def test_symbol_is_normalised():
    fake = FakeVenues(EIGHT_H_VENUES)
    async with fake.client() as c:
        r = await fs.fetch(c, {"symbol": " btcusdt "}, now_ms=NOW)
    assert r["asset"] == "BTC"


@pytest.mark.asyncio
async def test_dispatcher_routes_and_caches(monkeypatch):
    from gateway.services import tools_runtime, cache as cache_mod
    calls = {"n": 0}

    async def fake_fetch(client, params, **kw):
        calls["n"] += 1
        return {"asset": "BTC", "spread": {"pct_8h": 0.01}}

    monkeypatch.setattr(fs, "fetch", fake_fetch)
    cache_mod._cache.clear()
    r1 = await tools_runtime.real_tool_response("funding_spread", {"symbol": "BTC"})
    r2 = await tools_runtime.real_tool_response("funding_spread", {"symbol": "BTC"})
    assert r1["spread"]["pct_8h"] == 0.01 and r2.get("cached") is True
    assert calls["n"] == 1


def test_registry_entry_is_paid_and_discoverable():
    import registry
    from gateway.routes import tools as rt
    t = registry.get_tool("funding_spread")
    assert t and t.price_usdc == "0.01" and t.parameters["required"] == ["symbol"]
    assert "funding_spread" in rt._TOOL_BAZAAR
    assert "funding_spread" in {x["tool"] for x in rt._PAID_RELATED["pre_trade_check"]}
