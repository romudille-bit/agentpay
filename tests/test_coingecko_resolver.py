"""
test_coingecko_resolver.py — symbol to CoinGecko coin for token_price and
token_market_data.

A bare symbol used to be sent as a CoinGecko id, and ids like "wif" or "ape"
belong to other coins. The resolver uses the id map, then CoinGecko search.
"""
import httpx
import pytest

from gateway.services import tools_runtime as tr

SEARCH = {
    "wif": [{"id": "wif", "symbol": "WIF", "name": "Wif", "market_cap_rank": None},
            {"id": "world-is-flat", "symbol": "WIF", "name": "World Is Flat", "market_cap_rank": 2875},
            {"id": "dogwifcoin", "symbol": "WIF", "name": "dogwifhat", "market_cap_rank": 203}],
    "dogwifcoin": [{"id": "dogwifcoin", "symbol": "WIF", "name": "dogwifhat", "market_cap_rank": 203}],
    "hype": [{"id": "hyperliquid", "symbol": "HYPE", "name": "Hyperliquid", "market_cap_rank": 10}],
}
PRICES = {"dogwifcoin": {"usd": 0.1318, "usd_24h_change": None, "usd_market_cap": 131_000_000},
          "wif": {"usd": 0.00000166, "usd_24h_change": 0.12, "usd_market_cap": 1660},
          "bitcoin": {"usd": 64537, "usd_24h_change": -1.2, "usd_market_cap": 1.27e12}}


class CoinGecko:
    def __init__(self):
        self.seen: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        p, q = req.url.path, req.url.params
        if p.endswith("/search"):
            return httpx.Response(200, json={"coins": SEARCH.get(q["query"], [])})
        if p.endswith("/simple/price"):
            ids = q["ids"]
            return httpx.Response(200, json={ids: PRICES[ids]} if ids in PRICES else {})
        if "/coins/" in p:
            return httpx.Response(404, json={"error": "coin not found"})
        return httpx.Response(404, text=f"unmocked {p}")

    def paths(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.seen]


@pytest.fixture(autouse=True)
def _empty_cache():
    tr._CG_COIN_CACHE.clear()
    yield
    tr._CG_COIN_CACHE.clear()


async def _run(fetcher, cg: CoinGecko, params: dict) -> dict:
    async with httpx.AsyncClient(transport=httpx.MockTransport(cg.handler)) as client:
        return await fetcher(client, params)


class TestTokenPrice:
    async def test_ticker_resolves_to_the_best_ranked_coin_not_the_same_named_id(self):
        cg = CoinGecko()
        out = await _run(tr._fetch_token_price, cg, {"symbol": "WIF"})
        assert out["coin_id"] == "dogwifcoin" and out["name"] == "dogwifhat"
        assert out["price_usd"] == 0.1318

    async def test_missing_24h_change_does_not_crash(self):
        out = await _run(tr._fetch_token_price, CoinGecko(), {"symbol": "WIF"})
        assert out["change_24h_pct"] == 0

    async def test_mapped_symbol_skips_the_search(self):
        cg = CoinGecko()
        out = await _run(tr._fetch_token_price, cg, {"symbol": "BTC"})
        assert out["coin_id"] == "bitcoin" and cg.paths() == ["price"]

    async def test_a_coingecko_id_still_works_as_input(self):
        out = await _run(tr._fetch_token_price, CoinGecko(), {"symbol": "dogwifcoin"})
        assert out["coin_id"] == "dogwifcoin"

    async def test_unknown_symbol_is_an_error_and_no_price_is_fetched(self):
        cg = CoinGecko()
        out = await _run(tr._fetch_token_price, cg, {"symbol": "NOTACOIN"})
        assert "not found" in out["error"] and cg.paths() == ["search"]

    async def test_resolution_is_cached(self):
        cg = CoinGecko()
        async with httpx.AsyncClient(transport=httpx.MockTransport(cg.handler)) as client:
            await tr._fetch_token_price(client, {"symbol": "WIF"})
            await tr._fetch_token_price(client, {"symbol": "wif"})
        assert cg.paths().count("search") == 1


class TestTokenMarketData:
    async def test_unknown_symbol_is_a_clean_error(self):
        out = await _run(tr._fetch_dex_liquidity, CoinGecko(), {"token_a": "NOTACOIN"})
        assert out == {"error": "Token 'notacoin' not found on CoinGecko"}

    async def test_a_404_from_coingecko_is_a_clean_error(self):
        out = await _run(tr._fetch_dex_liquidity, CoinGecko(), {"token_a": "HYPE"})
        assert out == {"error": "Token 'hype' not found on CoinGecko"}
