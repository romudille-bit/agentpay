"""
registry.py — Tool registry for AgentPay.

Stores tool metadata: name, endpoint, price, developer wallet.
MVP uses in-memory dict. Swap for Supabase in production.
"""

from dataclasses import dataclass, asdict, field
from typing import Optional, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str                   # Unique tool identifier, e.g. "token_price"
    description: str            # What the tool does
    endpoint: str               # Internal URL of the actual MCP tool
    price_usdc: str             # Price per call in USDC, e.g. "0.001"
    developer_address: str      # Stellar wallet of tool developer
    parameters: dict            # JSON schema of input parameters
    category: str = "data"      # data | defi | trading | monitoring
    uptime_pct: float = 100.0   # Running uptime percentage
    total_calls: int = 0        # Lifetime call count
    active: bool = True
    triggers: list = field(default_factory=list)   # Keywords that should cause an agent to consider this tool
    use_when: str = ""                              # Plain English: when to call this tool
    returns: str = ""                              # What the tool gives back
    response_example: Any = field(default=None)    # Real example of the response shape


# ── Seed Data (MVP hardcoded tools) ──────────────────────────────────────────
# In production these come from Supabase

_TOOLS: dict[str, Tool] = {
    "token_price": Tool(
        name="token_price",
        description="Get the current USD price of any cryptocurrency token",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/token_price",
        price_usdc="0.001",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Token symbol, e.g. BTC, ETH, SOL",
                }
            },
            "required": ["symbol"],
        },
        category="data",
        triggers=["price", "how much is", "what is btc", "token value", "crypto price", "usd", "market price", "worth"],
        use_when="You need the current USD price, 24h change, or market cap of any cryptocurrency.",
        returns="price_usd, change_24h_pct, market_cap_usd, coin_id",
        response_example={"symbol": "ETH", "price_usd": 2069.73, "change_24h_pct": -4.04, "market_cap_usd": 250330787714.19, "source": "coingecko"},
    ),
    "wallet_balance": Tool(
        name="wallet_balance",
        description="Get the token balances for any Ethereum or Stellar wallet address",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/wallet_balance",
        price_usdc="0.002",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "Wallet address (Ethereum 0x... or Stellar G...)",
                },
                "chain": {
                    "type": "string",
                    "enum": ["ethereum", "stellar"],
                    "description": "Blockchain to query",
                },
            },
            "required": ["address", "chain"],
        },
        category="data",
        triggers=["wallet", "balance", "holdings", "portfolio", "address", "how much does", "tokens in wallet"],
        use_when="You need to look up the token holdings of an Ethereum or Stellar wallet address.",
        returns="list of token balances (symbol, amount) for the given address",
        response_example={"address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "chain": "ethereum", "balances": [{"token": "ETH", "amount": "1.234"}, {"token": "USDC", "contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "amount": "500.00"}]},
    ),
    "token_market_data": Tool(
        name="token_market_data",
        description="Get market cap, 24h volume, ATH, and price change for any token. Note: does NOT return pool depth or slippage — for pre-trade liquidity estimates, use a dedicated orderbook tool.",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/token_market_data",
        price_usdc="0.001",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "token_a": {"type": "string", "description": "First token symbol"},
                "token_b": {"type": "string", "description": "Second token symbol, e.g. USDC"},
            },
            "required": ["token_a", "token_b"],
        },
        category="data",
        triggers=["market cap", "volume", "trading volume", "all-time high", "ath", "market data", "24h volume", "price change"],
        use_when="You need 24h trading volume, market cap, ATH, or price change for a token. Not for DEX pool depth or slippage.",
        returns="volume_24h_usd, volume_change_24h_pct, market_cap_usd, price_usd, ath_usd, price_change_24h_pct",
        response_example={"token_a": "ETH", "token_b": "USDC", "price_usd": 2071.45, "volume_24h_usd": 312847293.0, "volume_change_24h_pct": -8.3, "market_cap_usd": 249800000000.0, "ath_usd": 4878.26, "price_change_24h_pct": -3.91, "source": "coingecko"},
    ),
    "gas_tracker": Tool(
        name="gas_tracker",
        description="Get current Ethereum gas prices (slow, standard, fast)",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/gas_tracker",
        price_usdc="0.001",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {},
        },
        category="data",
        triggers=["gas", "gwei", "transaction fee", "ethereum fee", "network congestion", "gas price", "eth fee"],
        use_when="You need to know current Ethereum gas prices before submitting a transaction or estimating costs.",
        returns="slow_gwei, standard_gwei, fast_gwei, base_fee_gwei, estimated confirmation times",
        response_example={"slow_gwei": 1.5, "standard_gwei": 2.0, "fast_gwei": 3.0, "base_fee_gwei": 1.2, "source": "etherscan"},
    ),
    "dune_query": Tool(
        name="dune_query",
        description="Run any Dune Analytics query and return live onchain results by query ID. Use fast_only=True for live bots — returns cached result instantly or raises immediately, never blocks.",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/dune_query",
        price_usdc="0.005",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "query_id": {
                    "type": "integer",
                    "description": "Dune Analytics query ID (visible in the query URL)",
                },
                "query_parameters": {
                    "type": "object",
                    "description": "Optional named parameters to pass to the Dune query",
                    "default": {},
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 25)",
                    "default": 25,
                },
                "fast_only": {
                    "type": "boolean",
                    "description": "If True, return cached result immediately or raise — never execute a fresh query. Use for live bots where latency matters. Default: False.",
                    "default": False,
                },
            },
            "required": ["query_id"],
        },
        category="data",
        triggers=["dune", "onchain", "sql", "analytics", "custom query", "blockchain data", "onchain metrics", "protocol stats"],
        use_when="You need deep onchain analytics from a specific Dune query — protocol revenue, user counts, custom metrics.",
        returns="rows[], columns[], row_count, generated_at from the Dune Analytics query result",
        response_example={"query_id": 3810512, "row_count": 2, "columns": ["protocol", "revenue_usd"], "rows": [{"protocol": "Uniswap V3", "revenue_usd": 1243800.0}, {"protocol": "Aave V3", "revenue_usd": 987200.0}], "generated_at": "2026-03-22T00:00:00Z", "source": "dune"},
    ),
    "fear_greed_index": Tool(
        name="fear_greed_index",
        description="Crypto Fear & Greed Index (0=extreme fear, 100=extreme greed) with optional history",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/fear_greed_index",
        price_usdc="0.001",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of days of history to return (default 1, max 30)",
                    "default": 1,
                },
            },
        },
        category="data",
        triggers=["fear", "greed", "sentiment", "market mood", "investor sentiment", "bullish", "bearish", "panic", "fomo"],
        use_when="You need to gauge overall crypto market sentiment or mood — whether the market is fearful or greedy.",
        returns="value (0–100), value_classification (e.g. 'Greed'), optional history[]",
        response_example={"value": 10, "value_classification": "Extreme Fear", "timestamp": 1774137600, "source": "alternative.me"},
    ),
    "crypto_news": Tool(
        name="crypto_news",
        description="Latest crypto news and community sentiment from r/CryptoCurrency for any token",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/crypto_news",
        price_usdc="0.003",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "currencies": {
                    "type": "string",
                    "description": "Comma-separated token symbols, e.g. 'BTC,ETH'",
                    "default": "BTC,ETH",
                },
                "filter": {
                    "type": "string",
                    "enum": ["hot", "new", "rising", "top"],
                    "description": "Feed sort order (default: hot)",
                    "default": "hot",
                },
            },
        },
        category="data",
        triggers=["news", "headlines", "what's happening", "latest", "trending", "community", "reddit", "narrative", "buzz"],
        use_when="You need recent news headlines or community sentiment for one or more crypto tokens.",
        returns="headlines[] with title, url, sentiment (bullish/neutral/bearish), score, published_at",
        response_example={"currencies": "ETH", "headlines": [{"title": "Ethereum devs confirm Pectra upgrade timeline", "url": "https://reddit.com/r/CryptoCurrency/...", "sentiment": "bullish", "score": 1842, "published_at": "2026-03-22T08:14:00Z"}, {"title": "ETH gas fees drop to yearly lows", "url": "https://reddit.com/r/CryptoCurrency/...", "sentiment": "bullish", "score": 934, "published_at": "2026-03-22T06:31:00Z"}], "source": "reddit"},
    ),
    "defi_tvl": Tool(
        name="defi_tvl",
        description="DeFi protocol Total Value Locked from DeFiLlama. Returns top 10 or a specific protocol.",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/defi_tvl",
        price_usdc="0.002",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "protocol": {
                    "type": "string",
                    "description": "Protocol name or slug, e.g. 'uniswap', 'aave', 'lido'. Leave empty for top 10.",
                    "default": "",
                },
            },
        },
        category="defi",
        triggers=["tvl", "total value locked", "defi", "protocol", "aave", "uniswap", "lido", "compound", "locked funds"],
        use_when="You need the Total Value Locked in a specific DeFi protocol or want to compare the top protocols by TVL.",
        returns="tvl, change_1h, change_1d, change_7d, chains[], category for the protocol (or top 10 list)",
        response_example={"protocol": "aave", "tvl": 23800000000.0, "change_1h": 0.12, "change_1d": -1.43, "change_7d": 3.21, "chains": ["Ethereum", "Polygon", "Avalanche", "Base"], "category": "Lending", "source": "defillama"},
    ),
    "whale_activity": Tool(
        name="whale_activity",
        description="Detect recent large wallet movements for a token (whale tracking)",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/whale_activity",
        price_usdc="0.002",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Token symbol to track"},
                "min_usd": {
                    "type": "number",
                    "description": "Minimum transaction size in USD",
                    "default": 100000,
                },
            },
            "required": ["token"],
        },
        category="monitoring",
        triggers=["whale", "large transfer", "big move", "institutional", "smart money", "accumulation", "dump", "sell-off"],
        use_when="You need to detect large token transfers that may signal institutional moves, accumulation, or sell-offs.",
        returns="large_transfers[] with from, to, amount, usd_value, minutes_ago; total_volume_usd",
        response_example={"token": "USDC", "large_transfers": [{"from": "0xabc...1234", "to": "0xdef...5678", "amount": 5000000.0, "usd_value": 5000000.0, "minutes_ago": 12}, {"from": "0x111...aaaa", "to": "0x222...bbbb", "amount": 2500000.0, "usd_value": 2500000.0, "minutes_ago": 34}], "total_volume_usd": 7500000.0, "source": "etherscan"},
    ),
    "yield_scanner": Tool(
        name="yield_scanner",
        description="Find best DeFi yield opportunities across protocols for a given token",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/yield_scanner",
        price_usdc="0.004",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Token symbol to find yields for, e.g. 'ETH', 'USDC', 'BTC'",
                },
                "chain": {
                    "type": "string",
                    "description": "Filter by chain: 'ethereum', 'base', 'arbitrum', 'polygon'. Leave empty for all chains.",
                    "default": "",
                },
                "min_tvl": {
                    "type": "number",
                    "description": "Minimum pool TVL in USD (default 1,000,000)",
                    "default": 1000000,
                },
            },
            "required": ["token"],
        },
        category="defi",
        triggers=["yield", "apy", "earn", "interest", "lending", "staking", "defi returns", "best rate"],
        use_when="You need to find the best yield/APY for a token across DeFi protocols.",
        returns="list of pools with protocol, apy, tvl_usd, chain, risk_level sorted by APY descending",
        response_example={"token": "USDC", "pools": [{"protocol": "morpho", "apy": 8.74, "tvl_usd": 312000000.0, "chain": "Ethereum", "risk_level": "low"}, {"protocol": "aave-v3", "apy": 5.21, "tvl_usd": 1840000000.0, "chain": "Ethereum", "risk_level": "low"}, {"protocol": "compound-v3", "apy": 4.87, "tvl_usd": 920000000.0, "chain": "Ethereum", "risk_level": "low"}], "source": "defillama"},
    ),
    "funding_rates": Tool(
        name="funding_rates",
        description="Get perpetual futures funding rates across Binance, Bybit, and OKX",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/funding_rates",
        price_usdc="0.003",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "description": "Token symbol, e.g. 'BTC', 'ETH'. Leave empty for all major assets.",
                    "default": "",
                },
            },
        },
        category="defi",
        triggers=["funding rate", "perp", "perpetual", "long", "short", "futures", "leverage sentiment"],
        use_when="You need funding rates to gauge leveraged market sentiment or cost of holding a perp position.",
        returns="funding_rate_pct, annualized_rate_pct, sentiment (bullish/neutral/bearish) per exchange",
        response_example={"asset": "BTC", "rates": [{"exchange": "binance", "funding_rate_pct": 0.012, "annualized_rate_pct": 13.14, "sentiment": "bullish"}, {"exchange": "bybit", "funding_rate_pct": 0.011, "annualized_rate_pct": 12.04, "sentiment": "bullish"}, {"exchange": "okx", "funding_rate_pct": 0.009, "annualized_rate_pct": 9.85, "sentiment": "neutral"}], "source": "binance/bybit/okx"},
    ),
    "token_security": Tool(
        name="token_security",
        description="Scan any token contract for honeypot, rug pull, and security risks",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/token_security",
        price_usdc="0.002",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "contract_address": {
                    "type": "string",
                    "description": "Token contract address (0x...)",
                },
                "chain": {
                    "type": "string",
                    "enum": ["ethereum", "bsc"],
                    "description": "Blockchain to query (default: ethereum)",
                    "default": "ethereum",
                },
            },
            "required": ["contract_address"],
        },
        category="security",
        triggers=["rug", "honeypot", "safe", "scam", "contract risk", "token security", "is this safe", "audit"],
        use_when="You need to check if a token contract is safe before trading or investing.",
        returns="risk_level, is_honeypot, buy_tax, sell_tax, holder_count, owner_address, is_mintable, can_take_back_ownership",
        response_example={"contract_address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "chain": "ethereum", "risk_level": "low", "is_honeypot": False, "buy_tax": 0.0, "sell_tax": 0.0, "holder_count": 842341, "is_mintable": False, "can_take_back_ownership": False, "source": "goplus"},
    ),
    "open_interest": Tool(
        name="open_interest",
        description="Get total open interest in perpetual futures for any asset, with 1h and 24h change rates. Pairs with funding_rates to complete the derivatives picture — rising OI with high funding = overcrowded position.",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/open_interest",
        price_usdc="0.002",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "description": "Token symbol, e.g. 'BTC', 'ETH', 'SOL'",
                    "default": "BTC",
                },
            },
        },
        category="defi",
        triggers=["open interest", "oi", "long short ratio", "futures positioning", "perp oi", "market positioning", "leverage"],
        use_when="You need to know total open interest in perpetual futures and whether it's rising or falling. Combine with funding_rates for a full derivatives picture.",
        returns="total_oi_usd, oi_change_1h_pct, oi_change_24h_pct, long_short_ratio, per-exchange breakdown",
        response_example={"asset": "ETH", "price_usd": 2069.73, "total_oi_usd": 8420000000.0, "oi_change_1h_pct": 1.2, "oi_change_24h_pct": 12.4, "long_short_ratio": 1.08, "exchanges": [{"exchange": "Binance", "oi_contracts": 4066123.5, "oi_change_1h_pct": 1.2, "oi_change_24h_pct": 12.4, "long_short_ratio": 1.08}], "source": "binance/bybit"},
    ),
    "orderbook_depth": Tool(
        name="orderbook_depth",
        description="Get real bid/ask depth and slippage estimates at $10k, $50k, and $250k notional from Binance and Bybit. Use before sizing a position to know if you can execute without moving the market.",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/orderbook_depth",
        price_usdc="0.002",
        developer_address="GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
        parameters={
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "description": "Token symbol, e.g. 'BTC', 'ETH', 'SOL'",
                    "default": "ETH",
                },
            },
            "required": ["asset"],
        },
        category="data",
        triggers=["slippage", "orderbook", "market depth", "liquidity", "can i sell", "execution cost", "bid ask", "spread", "fill price"],
        use_when="You need to estimate slippage before executing a large trade. Tells you how much a $10k, $50k, or $250k order will move the market.",
        returns="best_bid, best_ask, spread_pct, depth with slippage_pct at $10k/$50k/$250k notional, per-exchange best prices",
        response_example={"asset": "ETH", "pair": "ETH/USDT", "best_ask": 2071.5, "best_bid": 2071.2, "spread_pct": 0.0145, "depth": [{"notional_usd": 10000, "slippage_pct": 0.002, "executable": True}, {"notional_usd": 50000, "slippage_pct": 0.008, "executable": True}, {"notional_usd": 250000, "slippage_pct": 0.031, "executable": True}], "exchanges": [{"exchange": "Binance", "best_ask": 2071.5, "best_bid": 2071.2}, {"exchange": "Bybit", "best_ask": 2071.6, "best_bid": 2071.1}], "source": "binance/bybit"},
    ),
}


# ── Registry Functions ────────────────────────────────────────────────────────

def get_tool(name: str) -> Optional[Tool]:
    return _TOOLS.get(name)


def list_tools(category: Optional[str] = None) -> list[Tool]:
    tools = list(_TOOLS.values())
    if category:
        tools = [t for t in tools if t.category == category]
    return [t for t in tools if t.active]


def register_tool(tool: Tool) -> Tool:
    """Add a new tool to the registry."""
    if tool.name in _TOOLS:
        raise ValueError(f"Tool '{tool.name}' already exists")
    _TOOLS[tool.name] = tool
    logger.info(f"Registered tool: {tool.name} @ {tool.price_usdc} USDC")
    return tool


def increment_call_count(tool_name: str):
    if tool_name in _TOOLS:
        _TOOLS[tool_name].total_calls += 1


def reload_tools(tools: list) -> None:
    """Replace the in-memory registry with a fresh list (e.g. loaded from database)."""
    global _TOOLS
    _TOOLS = {t.name: t for t in tools}
    logger.info(f"Registry reloaded with {len(_TOOLS)} tools from database")


def tool_to_dict(tool: Tool) -> dict:
    return asdict(tool)
