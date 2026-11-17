"""
test_derivatives_fetchers.py — funding_rates and open_interest fetchers.

Both fetchers are replayed through httpx.MockTransport with the venues' real
response shapes, so the contract choice, the price fallback and the degrade
paths are checked without network. Before this file neither fetcher had a test:
every consumer test stubbed their output with a constant dict.
"""
import httpx
import pytest

from gateway.services import tools_runtime as tr
from registry.registry import _TOOLS


class Venues:
    """Canned venue responses. `down` maps a venue to the HTTP status it returns."""

    def __init__(self, *, okx_funding_inst="STX-USDT-SWAP", cg_price=None,
                 gate_row=None, down=None):
        self.okx_funding_inst = okx_funding_inst   # the only instId OKX lists
        self.cg_price = cg_price                   # {coin_id: usd} CoinGecko resolves
        self.gate_row = gate_row
        self.down = down or {}
        self.seen: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        h, p, q = req.url.host, req.url.path, req.url.params
        venue = {"fapi.binance.com": "binance", "api.bybit.com": "bybit",
                 "www.okx.com": "okx", "api.gateio.ws": "gate",
                 "api.bitget.com": "bitget"}.get(h, "coingecko")
        if venue in self.down:
            return httpx.Response(self.down[venue], text="blocked")

        if venue == "binance":
            if p.endswith("/premiumIndex"):
                return httpx.Response(200, json={
                    "symbol": q["symbol"], "lastFundingRate": "-0.0000931",
                    "nextFundingTime": 1794844800000})
            if p.endswith("/openInterest"):
                return httpx.Response(200, json={"openInterest": "21000000"})
            if p.endswith("/openInterestHist"):
                return httpx.Response(200, json=[{"sumOpenInterest": "20000000"}])
            if p.endswith("/globalLongShortAccountRatio"):
                return httpx.Response(200, json=[{"longShortRatio": "0.623"}])
        if venue == "bybit":
            if p.endswith("/tickers"):
                return httpx.Response(200, json={"result": {"list": [{
                    "symbol": q["symbol"], "fundingRate": "-0.00018967",
                    "nextFundingTime": "1794844800000"}]}})
            if p.endswith("/open-interest"):
                return httpx.Response(200, json={"result": {"list": [
                    {"openInterest": "20400000"}, {"openInterest": "20390000"}]}})
        if venue == "okx":
            if p.endswith("/funding-rate"):
                if q["instId"] != self.okx_funding_inst:
                    # What OKX really answers for an unlisted instrument: 200, not 4xx.
                    return httpx.Response(200, json={"code": "51001", "data": [], "msg": "Instrument ID doesn't exist."})
                return httpx.Response(200, json={"code": "0", "data": [{
                    "instId": q["instId"], "fundingRate": "-0.000143",
                    "nextFundingTime": "1794873600000"}]})
            if p.endswith("/public/open-interest"):
                return httpx.Response(200, json={"data": [{
                    "oiCcy": "7500000", "oiUsd": "1950000"}]})   # implies $0.26
            if p.endswith("/open-interest-history"):
                return httpx.Response(200, json={"data": [["0", "0", "7500000", "0"]] * 25})
            if p.endswith("/long-short-account-ratio"):
                return httpx.Response(200, json={"data": [["0", "2.43"]]})
        if venue == "gate":
            return httpx.Response(200, json=[self.gate_row] if self.gate_row else [])
        if venue == "bitget":
            return httpx.Response(200, json={"data": []})
        if venue == "coingecko":
            ids = q.get("ids", "")
            price = (self.cg_price or {}).get(ids)
            return httpx.Response(200, json={ids: {"usd": price}} if price else {})
        return httpx.Response(404, text=f"unmocked {h}{p}")


async def _run(fetcher, venues: Venues, params: dict) -> dict:
    async with httpx.AsyncClient(transport=httpx.MockTransport(venues.handler)) as client:
        return await fetcher(client, params)


def _okx_funding_inst(venues: Venues) -> str:
    return next(r.url.params["instId"] for r in venues.seen
                if r.url.host == "www.okx.com" and r.url.path.endswith("/funding-rate"))


class TestFundingRates:
    async def test_okx_reads_the_usdt_margined_swap(self):
        """The coin-margined STX-USD-SWAP is not listed, so OKX used to vanish."""
        v = Venues()
        out = await _run(tr._fetch_funding_rates, v, {"asset": "stx"})
        assert _okx_funding_inst(v) == "STX-USDT-SWAP"
        by_venue = {r["exchange"]: r for r in out["rates"]}
        assert set(by_venue) == {"Binance", "Bybit", "OKX"}
        assert by_venue["OKX"]["funding_rate_pct"] == pytest.approx(-0.0143)
        assert out["sources"] == ["Binance", "Bybit", "OKX"]

    async def test_sources_lists_only_venues_that_answered(self):
        """OKX's 200 + code 51001 + empty data is a venue that did not answer."""
        v = Venues(okx_funding_inst="NOTHING-LISTED")
        out = await _run(tr._fetch_funding_rates, v, {"asset": "STX"})
        assert {r["exchange"] for r in out["rates"]} == {"Binance", "Bybit"}
        assert out["sources"] == ["Binance", "Bybit"]
        assert out["count"] == 2

    async def test_sources_drops_geo_blocked_venues(self):
        v = Venues(down={"binance": 451, "bybit": 403})
        out = await _run(tr._fetch_funding_rates, v, {"asset": "STX"})
        assert out["sources"] == ["OKX"]

    async def test_all_venues_down_is_an_error_not_an_empty_list(self):
        v = Venues(down={"binance": 451, "bybit": 403, "okx": 503})
        out = await _run(tr._fetch_funding_rates, v, {"asset": "STX"})
        assert "error" in out and "rates" not in out


class TestOpenInterestPrice:
    async def test_stx_resolves_to_coingeckos_blockstack_id(self):
        v = Venues(cg_price={"blockstack": 0.2573})
        out = await _run(tr._fetch_open_interest, v, {"symbol": "STX"})
        cg = next(r for r in v.seen if r.url.path.endswith("/simple/price"))
        assert cg.url.params["ids"] == "blockstack"
        assert out["price_usd"] == 0.2573

    async def test_unknown_coingecko_id_falls_back_to_the_venue_implied_price(self):
        """An asset CoinGecko can't resolve must not null the price or switch
        total_oi_usd from the Binance basis to OKX-only."""
        v = Venues(cg_price=None)
        out = await _run(tr._fetch_open_interest, v, {"symbol": "STX"})
        assert out["price_usd"] == pytest.approx(0.26)
        assert out["primary"] == "binance"
        assert "Binance" in out["oi_venues"]

    async def test_no_price_anywhere_still_reports_okx_usd(self):
        """Pre-existing degrade path: Binance and Bybit geo-blocked, CoinGecko empty."""
        v = Venues(cg_price=None, down={"binance": 451, "bybit": 403})
        out = await _run(tr._fetch_open_interest, v, {"symbol": "STX"})
        assert out["primary"] == "okx"
        assert out["total_oi_usd"] == 1_950_000
        assert out["oi_venues"] == ["OKX"]


class TestOpenInterestTotal:
    async def _oi(self, **kw):
        v = Venues(cg_price={"blockstack": 0.25}, **kw)
        return await _run(tr._fetch_open_interest, v, {"symbol": "STX"})

    async def test_total_is_the_sum_of_the_venues_listed(self):
        out = await self._oi()
        assert out["oi_venues"] == ["Binance", "Bybit", "OKX"]
        # Binance and Bybit are coin units x price; OKX reports USD itself.
        assert out["total_oi_usd"] == 21_000_000 * 0.25 + 20_400_000 * 0.25 + 1_950_000
        by_venue = {e["exchange"]: e for e in out["exchanges"]}
        assert out["total_oi_usd"] == sum(by_venue[v]["oi_usd"] for v in out["oi_venues"])

    async def test_blocked_venues_leave_the_total_and_the_list_together(self):
        out = await self._oi(down={"binance": 451, "bybit": 403})
        assert out["oi_venues"] == ["OKX"]
        assert out["total_oi_usd"] == 1_950_000

    async def test_one_venue_down_is_visible_in_oi_venues(self):
        out = await self._oi(down={"bybit": 403})
        assert out["oi_venues"] == ["Binance", "OKX"]
        assert out["total_oi_usd"] == 21_000_000 * 0.25 + 1_950_000

    async def test_ls_only_venues_are_never_summed(self):
        out = await self._oi(gate_row={"lsr_account": 1.2})
        assert "Gate.io" not in out["oi_venues"]

    def test_registry_example_total_matches_its_venues(self):
        ex = _TOOLS["open_interest"].response_example
        by_venue = {e["exchange"]: e for e in ex["exchanges"]}
        assert ex["total_oi_usd"] == sum(by_venue[v]["oi_usd"] for v in ex["oi_venues"])


class TestGateTakerRatio:
    ROW = {"lsr_account": 1.2069, "top_lsr_size": 0.9381}

    async def _gate(self, **extra):
        v = Venues(cg_price={"blockstack": 0.2573}, gate_row={**self.ROW, **extra})
        out = await _run(tr._fetch_open_interest, v, {"symbol": "STX"})
        return next(e for e in out["exchanges"] if e["exchange"] == "Gate.io")

    async def test_bucket_with_no_taker_trades_is_none_not_zero(self):
        g = await self._gate(lsr_taker=0, long_taker_size=0, short_taker_size=0)
        assert g["taker_long_short"] is None
        assert g["long_short_ratio"] == 1.207          # the account ratio is untouched

    async def test_all_buy_bucket_is_none_not_zero(self):
        """Gate answers lsr_taker 0 for buys / 0 sells; the ratio is undefined."""
        g = await self._gate(lsr_taker=0, long_taker_size=49, short_taker_size=0)
        assert g["taker_long_short"] is None

    async def test_real_taker_ratio_is_kept(self):
        g = await self._gate(lsr_taker=0.1442, long_taker_size=59, short_taker_size=409)
        assert g["taker_long_short"] == 0.144

    async def test_all_sell_bucket_is_a_real_zero(self):
        g = await self._gate(lsr_taker=0, long_taker_size=0, short_taker_size=409)
        assert g["taker_long_short"] == 0.0


class TestRegistryExample:
    def test_funding_rates_example_agrees_with_the_code(self):
        """Agents read this example in the MCP tool description. It used to label
        +0.012% 'bullish', which the contrarian classifier never returns."""
        ex = _TOOLS["funding_rates"].response_example
        for row in ex["rates"]:
            pct = row["funding_rate_pct"]
            assert row["sentiment"] == tr._funding_sentiment(pct), row
            assert row["annualized_rate_pct"] == round(pct * 3 * 365, 2), row
        assert {r["sentiment"] for r in ex["rates"]} == {"bullish", "neutral", "bearish"}
        assert ex["sources"] == [r["exchange"] for r in ex["rates"]]
        assert ex["count"] == len(ex["rates"])
