"""
services/tools_runtime.py — Live API implementations for the 14 tools.

Each `_fetch_*` function calls the upstream API for one tool and returns
the response payload. `real_tool_response` dispatches by tool_name and
applies per-tool TTL caching from services.cache.

Constants live here because they're tool-runtime data — `_COINGECKO_IDS`,
`_ERC20_CONTRACTS`, and `_EXCHANGE_WALLETS` only matter inside fetcher
functions. Moving them anywhere else would force callers to import them
from a separate constants module just to read them back here.
"""

import asyncio
import logging

import httpx

from gateway.config import settings
from gateway.services.cache import CACHE_TTL, cache_get, cache_set

logger = logging.getLogger(__name__)


# CoinGecko symbol → coin ID
_COINGECKO_IDS: dict[str, str] = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
    "usdc": "usd-coin", "usdt": "tether", "bnb": "binancecoin",
    "xrp": "ripple", "ada": "cardano", "avax": "avalanche-2",
    "dot": "polkadot", "matic": "matic-network", "pol": "matic-network",
    "link": "chainlink", "uni": "uniswap", "aave": "aave",
    "doge": "dogecoin", "shib": "shiba-inu", "op": "optimism",
    "arb": "arbitrum", "atom": "cosmos", "near": "near",
}

# ERC-20 contract addresses for whale tracking
_ERC20_CONTRACTS: dict[str, str] = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "ETH":  "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH as proxy
    "LINK": "0x514910771af9ca656af840dff83e8264ecf986ca",
    "UNI":  "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
    "AAVE": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
    "SHIB": "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce",
}

# Known exchange hot wallet addresses (lowercase) → exchange name
# Sources: Etherscan labels, publicly documented exchange addresses
_EXCHANGE_WALLETS: dict[str, str] = {
    # Binance
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance",
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f": "Binance",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976": "Binance",
    "0x4976a4a02f38326660d17bf34b431dc6e2eb2327": "Binance",
    # Coinbase
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740": "Coinbase",
    "0x3cd751e6b0078be393132286c442345e5dc49699": "Coinbase",
    "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511": "Coinbase",
    "0xa090e606e30bd747d4e6245a1517ebe430f0057e": "Coinbase",
    # Kraken
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2": "Kraken",
    "0x0a869d79a7052c7f1b55a8ebabbea3420f0d1e13": "Kraken",
    "0xe853c56864a2ebe4576a807d26fdc4a0ada51919": "Kraken",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken",
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX",
    "0xa7efae728d2936e78bda97dc267687568dd593f3": "OKX",
    # Bybit
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit",
    "0x2b5634c42055806a59e9107ed44d43c426e58258": "Bybit",
    # Bitfinex
    "0x77134cbc06cb00b66f4c7e623d5fdbf6777635ec": "Bitfinex",
    "0x742d35cc6634c0532925a3b844bc454e4438f44e": "Bitfinex",
    # Gemini
    "0xd24400ae8bfebb18ca49be86258a3c749cf46853": "Gemini",
    "0x07ee55aa48bb72dcc6e9d78256648910de513eca": "Gemini",
    # Huobi
    "0xab5c66752a9e8167967685f1450532fb96d5d24f": "Huobi",
    "0x6748f50f686bfbca6fe8ad62b22228b87f31ff2b": "Huobi",
    # Gate.io
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate.io",
}


def classify_transfer_direction(from_addr: str, to_addr: str) -> tuple[str, str | None]:
    """Return (direction, exchange_name) for a transfer.

    Direction is one of: 'exchange_inflow', 'exchange_outflow', 'wallet_to_wallet'.
    exchange_name is the matched exchange or None.
    """
    from_lower = from_addr.lower()
    to_lower = to_addr.lower()
    to_exchange = _EXCHANGE_WALLETS.get(to_lower)
    from_exchange = _EXCHANGE_WALLETS.get(from_lower)
    if to_exchange:
        return "exchange_inflow", to_exchange
    if from_exchange:
        return "exchange_outflow", from_exchange
    return "wallet_to_wallet", None


async def real_tool_response(tool_name: str, params: dict) -> dict:
    # Build cache key (include params for tools where they matter)
    if tool_name in CACHE_TTL:
        cache_key = f"{tool_name}:{sorted(params.items())}"
        cached = cache_get(cache_key)
        if cached is not None:
            logger.debug(f"[CACHE] hit for {tool_name}")
            return cached
    else:
        cache_key = None

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            if tool_name == "token_price":
                result = await _fetch_token_price(client, params)
            elif tool_name == "wallet_balance":
                result = await _fetch_wallet_balance(client, params)
            elif tool_name == "gas_tracker":
                result = await _fetch_gas_tracker(client)
            elif tool_name in ("dex_liquidity", "token_market_data"):
                result = await _fetch_dex_liquidity(client, params)
            elif tool_name == "whale_activity":
                result = await _fetch_whale_activity(client, params)
            elif tool_name == "dune_query":
                result = await _fetch_dune_query(client, params)
            elif tool_name == "fear_greed_index":
                result = await _fetch_fear_greed_index(client, params)
            elif tool_name == "crypto_news":
                result = await _fetch_crypto_news(client, params)
            elif tool_name == "defi_tvl":
                result = await _fetch_defi_tvl(client, params)
            elif tool_name == "token_security":
                result = await _fetch_token_security(client, params)
            elif tool_name == "yield_scanner":
                result = await _fetch_yield_scanner(client, params)
            elif tool_name == "funding_rates":
                result = await _fetch_funding_rates(client, params)
            elif tool_name == "open_interest":
                result = await _fetch_open_interest(client, params)
            elif tool_name == "orderbook_depth":
                result = await _fetch_orderbook_depth(client, params)
            else:
                result = {"error": f"No real API implementation for tool: {tool_name}"}
        except Exception as e:
            logger.error(f"Real API error for {tool_name}: {e}")
            result = {"error": str(e)}

    if cache_key and "error" not in result:
        cache_set(cache_key, result, CACHE_TTL[tool_name])

    return result


async def _fetch_token_price(client: httpx.AsyncClient, params: dict) -> dict:
    symbol = params.get("symbol", "BTC").lower()
    coin_id = _COINGECKO_IDS.get(symbol, symbol)
    resp = await client.get(
        f"{settings.COINGECKO_API_URL}/simple/price",
        params={
            "ids": coin_id,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_market_cap": "true",
        },
    )
    resp.raise_for_status()
    data = resp.json().get(coin_id, {})
    if not data:
        return {"error": f"Token '{symbol}' not found on CoinGecko"}
    return {
        "symbol": symbol.upper(),
        "coin_id": coin_id,
        "price_usd": data.get("usd", 0),
        "change_24h_pct": round(data.get("usd_24h_change", 0), 4),
        "market_cap_usd": data.get("usd_market_cap", 0),
        "source": "coingecko",
    }


async def _fetch_wallet_balance(client: httpx.AsyncClient, params: dict) -> dict:
    address = params.get("address", "")
    chain = params.get("chain", "stellar")

    if chain == "stellar":
        resp = await client.get(f"https://horizon.stellar.org/accounts/{address}")
        if resp.status_code == 404:
            return {"error": f"Stellar account {address} not found"}
        resp.raise_for_status()
        raw = resp.json()
        balances = []
        for b in raw.get("balances", []):
            balances.append({
                "token": b.get("asset_code", "XLM"),
                "issuer": b.get("asset_issuer", "native"),
                "amount": b.get("balance", "0"),
            })
        return {
            "address": address,
            "chain": "stellar",
            "balances": balances,
            "sequence": raw.get("sequence"),
            "source": "stellar_horizon",
        }

    # Ethereum: fetch ETH balance + top ERC-20 balances via Etherscan
    if not settings.ETHERSCAN_API_KEY:
        return {"error": "ETHERSCAN_API_KEY not configured — get a free key at etherscan.io/register"}

    api_key = settings.ETHERSCAN_API_KEY
    base = "https://api.etherscan.io/v2/api"

    eth_resp = await client.get(base, params={
        "chainid": "1", "module": "account", "action": "balance",
        "address": address, "tag": "latest", "apikey": api_key,
    })
    eth_resp.raise_for_status()
    eth_wei = int(eth_resp.json().get("result", "0"))
    eth_amount = eth_wei / 1e18

    # Discover ERC-20 tokens via recent transfer history
    tok_resp = await client.get(base, params={
        "chainid": "1", "module": "account", "action": "tokentx",
        "address": address, "sort": "desc", "page": 1, "offset": 50,
        "apikey": api_key,
    })
    seen_tokens: dict[str, str] = {}  # symbol → contract
    if tok_resp.status_code == 200:
        for tx in tok_resp.json().get("result", []):
            sym = tx.get("tokenSymbol", "")
            if sym and sym not in seen_tokens:
                seen_tokens[sym] = tx.get("contractAddress", "")

    # Hardcoded decimals for common ERC-20 tokens; fall back to 18
    _ERC20_DECIMALS: dict[str, int] = {
        "USDC": 6, "USDT": 6, "WBTC": 8, "DAI": 18, "WETH": 18,
        "LINK": 18, "UNI": 18, "AAVE": 18, "SHIB": 18, "MATIC": 18,
        "CRV": 18, "MKR": 18, "SNX": 18, "COMP": 18, "LDO": 18,
    }

    # Fetch actual balance for each detected ERC-20 contract
    balances = [{"token": "ETH", "amount": str(round(eth_amount, 6))}]
    for sym, contract in list(seen_tokens.items())[:8]:
        bal_resp = await client.get(base, params={
            "chainid": "1", "module": "account", "action": "tokenbalance",
            "contractaddress": contract, "address": address,
            "tag": "latest", "apikey": api_key,
        })
        amount = None
        if bal_resp.status_code == 200:
            raw = bal_resp.json().get("result", "0")
            try:
                decimals = _ERC20_DECIMALS.get(sym.upper(), 18)
                amount = str(round(int(raw) / (10 ** decimals), 6))
            except (ValueError, TypeError):
                amount = None
        balances.append({"token": sym, "contract": contract, "amount": amount})

    return {
        "address": address,
        "chain": "ethereum",
        "balances": balances,
        "source": "etherscan",
    }


async def _fetch_gas_tracker(client: httpx.AsyncClient) -> dict:
    resp = await client.get(
        "https://api.etherscan.io/v2/api",
        params={
            "chainid": "1", "module": "gastracker", "action": "gasoracle",
            "apikey": settings.ETHERSCAN_API_KEY,
        },
    )
    resp.raise_for_status()
    result = resp.json().get("result", {})
    if isinstance(result, str):
        return {"error": f"Etherscan gas tracker error: {result}"}
    return {
        "slow_gwei": float(result.get("SafeGasPrice", 0)),
        "standard_gwei": float(result.get("ProposeGasPrice", 0)),
        "fast_gwei": float(result.get("FastGasPrice", 0)),
        "base_fee_gwei": float(result.get("suggestBaseFee", 0)),
        "estimated_times": {"slow": "~5 min", "standard": "~1 min", "fast": "~15 sec"},
        "source": "etherscan",
    }


async def _fetch_dex_liquidity(client: httpx.AsyncClient, params: dict) -> dict:
    """Handles both legacy 'dex_liquidity' and renamed 'token_market_data' calls."""
    token_a = params.get("token_a", "ETH").lower()
    token_b = params.get("token_b", "USDC").upper()
    coin_id = _COINGECKO_IDS.get(token_a, token_a)

    resp = await client.get(
        f"{settings.COINGECKO_API_URL}/coins/{coin_id}",
        params={"localization": "false", "tickers": "false",
                "community_data": "false", "developer_data": "false"},
    )
    resp.raise_for_status()
    data = resp.json()
    market = data.get("market_data", {})

    return {
        "token_a": token_a.upper(),
        "token_b": token_b,
        "price_usd": market.get("current_price", {}).get("usd", 0),
        "volume_24h_usd": market.get("total_volume", {}).get("usd", 0),
        "volume_change_24h_pct": round(market.get("total_volume_change_24h", 0) or 0, 2),
        "market_cap_usd": market.get("market_cap", {}).get("usd", 0),
        "price_change_24h_pct": round(market.get("price_change_percentage_24h", 0), 4),
        "ath_usd": market.get("ath", {}).get("usd", 0),
        "source": "coingecko",
    }


async def _fetch_whale_activity(client: httpx.AsyncClient, params: dict) -> dict:
    import time
    token = params.get("token", "ETH").upper()
    min_usd = float(params.get("min_usd", 100_000))

    if not settings.ETHERSCAN_API_KEY:
        return {"error": "ETHERSCAN_API_KEY not configured — get a free key at etherscan.io/register"}

    contract = _ERC20_CONTRACTS.get(token)
    if not contract:
        return {"error": f"Token {token} not supported. Supported: {list(_ERC20_CONTRACTS)}"}

    # Get price for USD value estimation
    coin_id = _COINGECKO_IDS.get(token.lower())
    price_usd = 0.0
    if coin_id:
        price_resp = await client.get(
            f"{settings.COINGECKO_API_URL}/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
        )
        if price_resp.status_code == 200:
            price_usd = price_resp.json().get(coin_id, {}).get("usd", 0)

    # Fetch recent transfers for this token contract
    resp = await client.get(
        "https://api.etherscan.io/v2/api",
        params={
            "chainid": "1", "module": "account", "action": "tokentx",
            "contractaddress": contract,
            "sort": "desc", "page": 1, "offset": 50,
            "apikey": settings.ETHERSCAN_API_KEY,
        },
    )
    resp.raise_for_status()
    txs = resp.json().get("result", [])
    if isinstance(txs, str):
        return {"error": f"Etherscan error: {txs}"}

    now = time.time()
    large_moves = []
    total_volume = 0.0
    direction_counts: dict[str, int] = {"exchange_inflow": 0, "exchange_outflow": 0, "wallet_to_wallet": 0}

    for tx in txs:
        try:
            decimals = int(tx.get("tokenDecimal", "18") or "18")
            amount = int(tx.get("value", "0")) / (10 ** decimals)
            if price_usd:
                usd_value = amount * price_usd
                if usd_value < min_usd:
                    continue  # reliably below threshold — skip
            else:
                usd_value = None  # price unavailable — include with null
            total_volume += usd_value or 0

            # Classify direction using full addresses before truncating
            from_addr = tx.get("from", "")
            to_addr = tx.get("to", "")
            direction, exchange_name = classify_transfer_direction(from_addr, to_addr)
            direction_counts[direction] = direction_counts.get(direction, 0) + 1

            large_moves.append({
                "from": from_addr[:10] + "..." if len(from_addr) > 10 else from_addr,
                "to": to_addr[:10] + "..." if len(to_addr) > 10 else to_addr,
                "amount": round(amount, 4),
                "token": tx.get("tokenSymbol", token),
                "usd_value": round(usd_value, 2) if usd_value else None,
                "direction": direction,
                "exchange_name": exchange_name,
                "minutes_ago": round((now - int(tx.get("timeStamp", now))) / 60),
                "tx_hash": tx.get("hash", "")[:18] + "...",
            })
        except Exception:
            continue

    return {
        "token": token,
        "min_usd_filter": min_usd,
        "price_usd": price_usd,
        "large_transfers": large_moves[:15],
        "total_volume_usd": round(total_volume, 2),
        "direction_summary": direction_counts,
        "source": "etherscan",
    }


async def _fetch_dune_query(client: httpx.AsyncClient, params: dict) -> dict:
    if not settings.DUNE_API_KEY:
        return {"error": "DUNE_API_KEY not configured"}

    query_id = params.get("query_id")
    if not query_id:
        return {"error": "query_id is required"}

    query_parameters = params.get("query_parameters", {})
    limit = int(params.get("limit", 25))
    fast_only = bool(params.get("fast_only", False))
    headers = {"X-Dune-API-Key": settings.DUNE_API_KEY}
    dune_base = "https://api.dune.com/api/v1"

    # Always try cached results first (fast path for live bots)
    cached = await client.get(
        f"{dune_base}/query/{query_id}/results",
        headers=headers,
        params={"limit": limit},
        timeout=15.0,
    )
    if cached.status_code == 200:
        data = cached.json()
        if data.get("is_execution_finished") and data.get("state") == "QUERY_STATE_COMPLETED":
            rows = data.get("result", {}).get("rows", [])
            cols = list(rows[0].keys()) if rows else []
            return {
                "query_id": query_id,
                "execution_id": data.get("execution_id"),
                "row_count": len(rows),
                "columns": cols,
                "rows": rows[:limit],
                "generated_at": data.get("execution_ended_at", ""),
                "source": "cached",
                "fast_only": fast_only,
            }

    # fast_only=True: never execute a fresh query — return immediately if no cache
    if fast_only:
        return {
            "error": "No cached results available for this query. Run without fast_only=True to execute fresh (may take up to 90s).",
            "query_id": query_id,
            "fast_only": True,
        }

    # fast_only=False (default): execute fresh query and poll up to 90s
    exec_resp = await client.post(
        f"{dune_base}/query/{query_id}/execute",
        headers=headers,
        json={"query_parameters": query_parameters},
        timeout=15.0,
    )
    if exec_resp.status_code != 200:
        return {"error": f"Dune execute failed: {exec_resp.text}"}

    execution_id = exec_resp.json().get("execution_id")

    # Poll up to 90s
    poll_client = httpx.AsyncClient(timeout=15.0)
    async with poll_client:
        for _ in range(45):
            await asyncio.sleep(2)
            poll = await poll_client.get(
                f"{dune_base}/execution/{execution_id}/results",
                headers=headers,
                params={"limit": limit},
            )
            if poll.status_code != 200:
                continue
            pdata = poll.json()
            state = pdata.get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                rows = pdata.get("result", {}).get("rows", [])
                cols = list(rows[0].keys()) if rows else []
                return {
                    "query_id": query_id,
                    "execution_id": execution_id,
                    "row_count": len(rows),
                    "columns": cols,
                    "rows": rows[:limit],
                    "generated_at": pdata.get("execution_ended_at", ""),
                    "source": "executed",
                    "fast_only": False,
                }
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                return {"error": f"Dune query {state}: {pdata.get('error', '')}"}

    return {"error": "Dune query timed out after 90s"}


async def _fetch_fear_greed_index(client: httpx.AsyncClient, params: dict) -> dict:
    limit = max(1, min(int(params.get("limit", 1)), 30))
    resp = await client.get(
        "https://api.alternative.me/fng/",
        params={"limit": limit, "format": "json"},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    entries = data.get("data", [])
    if not entries:
        return {"error": "No data returned from Fear & Greed API"}

    current = entries[0]
    result = {
        "value": int(current["value"]),
        "value_classification": current["value_classification"],
        "timestamp": int(current["timestamp"]),
        "source": "alternative.me",
    }
    if limit > 1:
        result["history"] = [
            {
                "value": int(e["value"]),
                "value_classification": e["value_classification"],
                "timestamp": int(e["timestamp"]),
            }
            for e in entries
        ]
    return result


async def _fetch_crypto_news(client: httpx.AsyncClient, params: dict) -> dict:
    currencies = params.get("currencies", "BTC,ETH")
    filter_type = params.get("filter", "hot")
    sort = filter_type if filter_type in ("hot", "new", "rising", "top") else "hot"

    # One subreddit query per currency token, merged and sorted by score
    tokens = [t.strip().lower() for t in currencies.split(",") if t.strip()]
    query = " OR ".join(tokens) if tokens else currencies

    resp = await client.get(
        "https://www.reddit.com/r/CryptoCurrency/search.json",
        params={"q": query, "sort": sort, "restrict_sr": "1", "limit": "10", "t": "week"},
        headers={"User-Agent": "AgentPay/1.0"},
        timeout=10.0,
    )
    resp.raise_for_status()
    posts = resp.json().get("data", {}).get("children", [])

    headlines = []
    for p in posts[:5]:
        d = p["data"]
        sentiment = "bullish" if d.get("upvote_ratio", 0.5) >= 0.65 else (
            "bearish" if d.get("upvote_ratio", 0.5) <= 0.35 else "neutral"
        )
        headlines.append({
            "title": d.get("title", ""),
            "url": d.get("url", ""),
            "source": d.get("domain", "reddit.com"),
            "sentiment": sentiment,
            "score": d.get("score", 0),
            "comments": d.get("num_comments", 0),
            "published_at": d.get("created_utc", 0),
        })

    return {
        "currencies": currencies,
        "filter": sort,
        "count": len(headlines),
        "headlines": headlines,
        "source": "reddit/r/CryptoCurrency",
    }


async def _fetch_defi_tvl(client: httpx.AsyncClient, params: dict) -> dict:
    protocol = params.get("protocol", "").strip().lower()

    resp = await client.get("https://api.llama.fi/protocols", timeout=15.0)
    resp.raise_for_status()
    protocols = resp.json()

    if protocol:
        matches = [
            p for p in protocols
            if protocol in p.get("slug", "").lower()
            or protocol in p.get("name", "").lower()
        ]
        if not matches:
            return {"error": f"Protocol '{protocol}' not found. Try 'uniswap', 'aave', 'lido', etc."}
        p = matches[0]
        return {
            "name": p.get("name"),
            "slug": p.get("slug"),
            "tvl": round(p.get("tvl") or 0, 2),
            "change_1h": p.get("change_1h"),
            "change_1d": p.get("change_1d"),
            "change_7d": p.get("change_7d"),
            "chains": p.get("chains", []),
            "category": p.get("category"),
            "source": "defillama",
        }

    # Top 10 by TVL
    top = sorted(protocols, key=lambda x: x.get("tvl") or 0, reverse=True)[:10]
    return {
        "top_protocols": [
            {
                "name": p.get("name"),
                "tvl": round(p.get("tvl") or 0, 2),
                "change_1d": p.get("change_1d"),
                "change_7d": p.get("change_7d"),
                "chain": p.get("chain"),
                "category": p.get("category"),
            }
            for p in top
        ],
        "source": "defillama",
    }


async def _fetch_token_security(client: httpx.AsyncClient, params: dict) -> dict:
    address = params.get("contract_address", "").strip().lower()
    chain   = params.get("chain", "ethereum").lower()

    chain_id = "56" if chain == "bsc" else "1"

    resp = await client.get(
        f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}",
        params={"contract_addresses": address},
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 1:
        return {"error": f"GoPlus API error: {data.get('message', 'unknown')}"}

    result = data.get("result", {}).get(address) or data.get("result", {}).get(address.lower())
    if not result:
        return {"error": f"No security data found for contract {address} on {chain}"}

    def _int(val, default=0) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _float(val, default=0.0) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    is_honeypot             = _int(result.get("is_honeypot"))
    is_mintable             = _int(result.get("is_mintable"))
    is_proxy                = _int(result.get("is_proxy"))
    can_take_back_ownership = _int(result.get("can_take_back_ownership"))
    is_blacklisted          = _int(result.get("is_blacklisted"))
    is_whitelisted          = _int(result.get("is_whitelisted"))
    holder_count            = _int(result.get("holder_count"))
    lp_holders              = result.get("lp_holders", [])

    # GoPlus returns tax as decimal fraction: 0.05 = 5%
    buy_tax_pct  = round(_float(result.get("buy_tax"))  * 100, 2)
    sell_tax_pct = round(_float(result.get("sell_tax")) * 100, 2)

    # ── Risk level ────────────────────────────────────────────────────────────
    if is_honeypot or buy_tax_pct > 10 or sell_tax_pct > 10 or can_take_back_ownership:
        risk_level = "danger"
    elif is_mintable or is_proxy or buy_tax_pct > 5 or sell_tax_pct > 5 or is_blacklisted:
        risk_level = "caution"
    else:
        risk_level = "safe"

    return {
        "contract_address":        address,
        "chain":                   chain,
        "risk_level":              risk_level,
        "is_honeypot":             is_honeypot,
        "is_mintable":             is_mintable,
        "is_proxy":                is_proxy,
        "can_take_back_ownership": can_take_back_ownership,
        "is_blacklisted":          is_blacklisted,
        "is_whitelisted":          is_whitelisted,
        "buy_tax":                 buy_tax_pct,
        "sell_tax":                sell_tax_pct,
        "holder_count":            holder_count,
        "lp_holder_count":         len(lp_holders),
        "owner_address":           result.get("owner_address", ""),
        "creator_address":         result.get("creator_address", ""),
        "source":                  "gopluslabs",
    }


async def _fetch_yield_scanner(client: httpx.AsyncClient, params: dict) -> dict:
    token   = params.get("token", "").strip().upper()
    chain   = params.get("chain", "").strip().lower()
    min_tvl = float(params.get("min_tvl", 1_000_000))

    if not token:
        return {"error": "token parameter is required (e.g. 'ETH', 'USDC')"}

    resp = await client.get("https://yields.llama.fi/pools", timeout=20.0)
    resp.raise_for_status()
    pools = resp.json().get("data", [])

    # Filter: symbol contains token, TVL >= min_tvl, apy > 0, not outlier
    matched = [
        p for p in pools
        if token in p.get("symbol", "").upper()
        and (p.get("tvlUsd") or 0) >= min_tvl
        and (p.get("apy") or 0) > 0
        and not p.get("outlier", False)
    ]

    if chain:
        matched = [p for p in matched if p.get("chain", "").lower() == chain]

    if not matched:
        return {"error": f"No yield pools found for {token}" + (f" on {chain}" if chain else "") + f" with TVL >= ${min_tvl:,.0f}"}

    # Sort by APY descending, take top 10
    matched.sort(key=lambda p: p.get("apy") or 0, reverse=True)
    top = matched[:10]

    def risk(tvl: float) -> str:
        if tvl >= 100_000_000:
            return "low"
        if tvl >= 10_000_000:
            return "medium"
        return "high"

    return {
        "token":      token,
        "chain":      chain or "all",
        "min_tvl":    min_tvl,
        "pool_count": len(matched),
        "pools": [
            {
                "protocol":   p.get("project"),
                "chain":      p.get("chain"),
                "symbol":     p.get("symbol"),
                "apy":        round(p.get("apy") or 0, 4),
                "apy_base":   round(p.get("apyBase") or 0, 4),
                "apy_reward": round(p.get("apyReward") or 0, 4),
                "tvl_usd":    round(p.get("tvlUsd") or 0, 2),
                "pool_id":    p.get("pool"),
                "il_risk":    p.get("ilRisk"),
                "risk_level": risk(p.get("tvlUsd") or 0),
            }
            for p in top
        ],
        "source": "defillama-yields",
    }


async def _fetch_funding_rates(client: httpx.AsyncClient, params: dict) -> dict:
    asset = params.get("asset", "").strip().upper()

    # Fetch from Binance, Bybit, OKX in parallel
    async def _binance() -> list[dict]:
        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        p   = {"symbol": f"{asset}USDT"} if asset else {}
        r   = await client.get(url, params=p, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else [data]
        results = []
        for item in rows:
            sym = item.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            ticker = sym[:-4]
            if asset and ticker != asset:
                continue
            rate = float(item.get("lastFundingRate") or 0)
            results.append({
                "asset":    ticker,
                "exchange": "Binance",
                "funding_rate_pct":    round(rate * 100, 6),
                "annualized_rate_pct": round(rate * 100 * 3 * 365, 2),
                "next_funding_time":   item.get("nextFundingTime"),
            })
        return results

    async def _bybit() -> list[dict]:
        sym = f"{asset}USDT" if asset else None
        p   = {"category": "linear", **({"symbol": sym} if sym else {})}
        r   = await client.get("https://api.bybit.com/v5/market/tickers", params=p, timeout=10.0)
        r.raise_for_status()
        items = r.json().get("result", {}).get("list", [])
        results = []
        for item in items:
            s = item.get("symbol", "")
            if not s.endswith("USDT"):
                continue
            ticker = s[:-4]
            rate   = float(item.get("fundingRate") or 0)
            results.append({
                "asset":    ticker,
                "exchange": "Bybit",
                "funding_rate_pct":    round(rate * 100, 6),
                "annualized_rate_pct": round(rate * 100 * 3 * 365, 2),
                "next_funding_time":   int(item.get("nextFundingTime") or 0),
            })
        return results

    async def _okx() -> list[dict]:
        # OKX requires a specific instId — only query if asset is given
        if not asset:
            return []
        inst_id = f"{asset}-USD-SWAP"
        r = await client.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=10.0,
        )
        r.raise_for_status()
        items = r.json().get("data", [])
        results = []
        for item in items:
            rate = float(item.get("fundingRate") or 0)
            results.append({
                "asset":    asset,
                "exchange": "OKX",
                "funding_rate_pct":    round(rate * 100, 6),
                "annualized_rate_pct": round(rate * 100 * 3 * 365, 2),
                "next_funding_time":   int(item.get("nextFundingTime") or 0),
            })
        return results

    # Run all three in parallel; ignore individual failures
    results_nested = await asyncio.gather(_binance(), _bybit(), _okx(), return_exceptions=True)
    rows: list[dict] = []
    for r in results_nested:
        if isinstance(r, list):
            rows.extend(r)

    if not rows:
        return {"error": f"Could not fetch funding rates" + (f" for {asset}" if asset else "")}

    # Keep top assets by absolute funding rate; limit to 30 rows when no asset specified
    if not asset:
        # Show major assets only
        major = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "ARB", "OP", "MATIC"}
        rows  = [r for r in rows if r["asset"] in major]

    rows.sort(key=lambda r: abs(r["funding_rate_pct"]), reverse=True)

    def sentiment(rate_pct: float) -> str:
        if rate_pct < -0.01:
            return "bullish"   # shorts pay longs → market leans long
        if rate_pct > 0.05:
            return "bearish"   # longs pay shorts → overcrowded longs
        return "neutral"

    for r in rows:
        r["sentiment"] = sentiment(r["funding_rate_pct"])

    return {
        "asset":    asset or "major",
        "rates":    rows,
        "count":    len(rows),
        "sources":  ["Binance", "Bybit", "OKX"],
    }


async def _fetch_open_interest(client: httpx.AsyncClient, params: dict) -> dict:
    """Total open interest + 1h/24h change across Binance and Bybit. Free public APIs."""
    # Registry schema uses "symbol" (e.g. "ETH" or "BTC"); fall back to "asset" for compat
    raw = params.get("symbol", params.get("asset", "BTC")).strip().upper()
    # Strip trailing USDT/BUSD if caller passed the full pair
    asset = raw.replace("USDT", "").replace("BUSD", "") or raw
    symbol_binance = f"{asset}USDT"
    symbol_bybit   = f"{asset}USDT"

    async def _binance_oi() -> dict | None:
        try:
            # Spot OI
            r = await client.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": symbol_binance},
                timeout=10.0,
            )
            r.raise_for_status()
            spot = r.json()
            oi_now = float(spot.get("openInterest", 0))

            # Historical OI for 1h change (5m intervals, last 13 candles = ~1h)
            hist = await client.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": symbol_binance, "period": "5m", "limit": 13},
                timeout=10.0,
            )
            hist.raise_for_status()
            hist_data = hist.json()
            oi_1h_ago = float(hist_data[0]["sumOpenInterest"]) if hist_data else None

            # 24h change via 1h intervals
            hist_24h = await client.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": symbol_binance, "period": "1h", "limit": 25},
                timeout=10.0,
            )
            hist_24h.raise_for_status()
            hist_24h_data = hist_24h.json()
            oi_24h_ago = float(hist_24h_data[0]["sumOpenInterest"]) if hist_24h_data else None

            # Long/short ratio
            ls = await client.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": symbol_binance, "period": "5m", "limit": 1},
                timeout=10.0,
            )
            ls_ratio = None
            if ls.status_code == 200:
                ls_data = ls.json()
                if ls_data:
                    ls_ratio = round(float(ls_data[-1].get("longShortRatio", 0)), 3)

            return {
                "exchange":        "Binance",
                "oi_contracts":    round(oi_now, 2),
                "oi_change_1h_pct":  round((oi_now - oi_1h_ago) / oi_1h_ago * 100, 2) if oi_1h_ago else None,
                "oi_change_24h_pct": round((oi_now - oi_24h_ago) / oi_24h_ago * 100, 2) if oi_24h_ago else None,
                "long_short_ratio":  ls_ratio,
            }
        except Exception as e:
            logger.warning(f"open_interest Binance error: {e}")
            return None

    async def _bybit_oi() -> dict | None:
        try:
            r = await client.get(
                "https://api.bybit.com/v5/market/open-interest",
                params={"category": "linear", "symbol": symbol_bybit,
                        "intervalTime": "5min", "limit": 2},
                timeout=10.0,
            )
            r.raise_for_status()
            items = r.json().get("result", {}).get("list", [])
            if not items:
                return None
            oi_now    = float(items[0].get("openInterest", 0))
            oi_prev   = float(items[1].get("openInterest", 0)) if len(items) > 1 else None
            return {
                "exchange":           "Bybit",
                "oi_contracts":       round(oi_now, 2),
                "oi_change_5m_pct":   round((oi_now - oi_prev) / oi_prev * 100, 2) if oi_prev else None,
            }
        except Exception as e:
            logger.warning(f"open_interest Bybit error: {e}")
            return None

    # Fetch price to convert contracts → USD
    async def _price_usd() -> float:
        try:
            r = await client.get(
                f"{settings.COINGECKO_API_URL}/simple/price",
                params={"ids": _COINGECKO_IDS.get(asset.lower(), asset.lower()),
                        "vs_currencies": "usd"},
                timeout=8.0,
            )
            r.raise_for_status()
            coin_id = _COINGECKO_IDS.get(asset.lower(), asset.lower())
            return float(r.json().get(coin_id, {}).get("usd", 0))
        except Exception:
            return 0.0

    binance_res, bybit_res, price = await asyncio.gather(
        _binance_oi(), _bybit_oi(), _price_usd()
    )

    exchanges = [e for e in [binance_res, bybit_res] if e]
    if not exchanges:
        return {"error": f"Could not fetch open interest for {asset}"}

    # Aggregate total OI in USD using Binance as primary (contracts × price)
    total_oi_usd = None
    if binance_res and price:
        total_oi_usd = round(binance_res["oi_contracts"] * price, 0)

    return {
        "asset":              asset,
        "price_usd":          price or None,
        "total_oi_usd":       total_oi_usd,
        "oi_change_1h_pct":   binance_res.get("oi_change_1h_pct") if binance_res else None,
        "oi_change_24h_pct":  binance_res.get("oi_change_24h_pct") if binance_res else None,
        "long_short_ratio":   binance_res.get("long_short_ratio") if binance_res else None,
        "exchanges":          exchanges,
        "source":             "binance/bybit",
    }


async def _fetch_orderbook_depth(client: httpx.AsyncClient, params: dict) -> dict:
    """Real bid/ask depth + slippage at $10k, $50k, $250k notional. Free public APIs."""
    # Registry schema uses "symbol" = full pair e.g. "ETHUSDT"; accept bare asset too
    raw    = params.get("symbol", params.get("asset", "ETHUSDT")).strip().upper()
    symbol = raw if raw.endswith("USDT") else f"{raw}USDT"
    asset  = symbol.replace("USDT", "")
    exchange_pref = params.get("exchange", "binance").lower()

    async def _binance_book() -> list[dict] | None:
        try:
            r = await client.get(
                "https://api.binance.com/api/v3/depth",
                params={"symbol": symbol, "limit": 100},
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json()
            return {
                "exchange": "Binance",
                "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
                "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
            }
        except Exception as e:
            logger.warning(f"orderbook Binance error: {e}")
            return None

    async def _bybit_book() -> dict | None:
        try:
            r = await client.get(
                "https://api.bybit.com/v5/market/orderbook",
                params={"category": "spot", "symbol": symbol, "limit": 50},
                timeout=10.0,
            )
            r.raise_for_status()
            result = r.json().get("result", {})
            return {
                "exchange": "Bybit",
                "bids": [[float(p), float(q)] for p, q in result.get("b", [])],
                "asks": [[float(p), float(q)] for p, q in result.get("a", [])],
            }
        except Exception as e:
            logger.warning(f"orderbook Bybit error: {e}")
            return None

    def _calc_slippage(asks: list, notional_usd: float) -> float | None:
        """Walk the ask side to estimate avg fill price vs best ask."""
        if not asks:
            return None
        best_ask  = asks[0][0]
        remaining = notional_usd
        total_cost = 0.0
        total_qty  = 0.0
        for price, qty in asks:
            cost = price * qty
            if cost >= remaining:
                fill_qty   = remaining / price
                total_cost += remaining
                total_qty  += fill_qty
                remaining   = 0
                break
            total_cost += cost
            total_qty  += qty
            remaining  -= cost
        if remaining > 0:
            return None  # not enough liquidity
        avg_fill = total_cost / total_qty
        return round((avg_fill - best_ask) / best_ask * 100, 4)

    binance_book, bybit_book = await asyncio.gather(_binance_book(), _bybit_book())

    # Prefer exchange requested by caller; fall back to whichever is available
    if exchange_pref == "bybit":
        book = bybit_book or binance_book
    else:
        book = binance_book or bybit_book
    if not book:
        return {"error": f"Could not fetch orderbook for {asset}"}

    asks = book["asks"]
    bids = book["bids"]
    best_ask = asks[0][0] if asks else None
    best_bid = bids[0][0] if bids else None
    spread_pct = round((best_ask - best_bid) / best_ask * 100, 4) if best_ask and best_bid else None

    notionals = [10_000, 50_000, 250_000]
    depth = []
    for n in notionals:
        slip = _calc_slippage(asks, n)
        depth.append({
            "notional_usd":  n,
            "slippage_pct":  slip,
            "executable":    slip is not None,
        })

    # Also aggregate both exchanges for best_bid/ask comparison
    exchange_summary = []
    for b in [binance_book, bybit_book]:
        if b and b.get("asks") and b.get("bids"):
            exchange_summary.append({
                "exchange":  b["exchange"],
                "best_ask":  b["asks"][0][0],
                "best_bid":  b["bids"][0][0],
            })

    return {
        "asset":       asset,
        "pair":        f"{asset}/USDT",
        "exchange":    book["exchange"],
        "best_ask":    best_ask,
        "best_bid":    best_bid,
        "spread_pct":  spread_pct,
        "depth":       depth,
        "exchanges":   exchange_summary,
        "source":      "binance/bybit",
    }
