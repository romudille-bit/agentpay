"""
registry.py — Tool registry for AgentPay.

Stores tool metadata: name, endpoint, price, developer wallet.
MVP uses in-memory dict. Swap for Supabase in production.
"""

from dataclasses import dataclass, asdict, field
from typing import Optional
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


# ── Seed Data (MVP hardcoded tools) ──────────────────────────────────────────
# In production these come from Supabase

_TOOLS: dict[str, Tool] = {
    "token_price": Tool(
        name="token_price",
        description="Get the current USD price of any cryptocurrency token",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/token_price",
        price_usdc="0.001",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
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
    ),
    "wallet_balance": Tool(
        name="wallet_balance",
        description="Get the token balances for any Ethereum or Stellar wallet address",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/wallet_balance",
        price_usdc="0.002",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
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
    ),
    "dex_liquidity": Tool(
        name="dex_liquidity",
        description="Get liquidity depth and volume for a token pair on DEXs",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/dex_liquidity",
        price_usdc="0.003",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
        parameters={
            "type": "object",
            "properties": {
                "token_a": {"type": "string", "description": "First token symbol"},
                "token_b": {"type": "string", "description": "Second token symbol, e.g. USDC"},
            },
            "required": ["token_a", "token_b"],
        },
        category="defi",
    ),
    "gas_tracker": Tool(
        name="gas_tracker",
        description="Get current Ethereum gas prices (slow, standard, fast)",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/gas_tracker",
        price_usdc="0.001",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
        parameters={
            "type": "object",
            "properties": {},
        },
        category="data",
    ),
    "dune_query": Tool(
        name="dune_query",
        description="Run any Dune Analytics query and return live onchain results by query ID",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/dune_query",
        price_usdc="0.005",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
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
            },
            "required": ["query_id"],
        },
        category="data",
    ),
    "fear_greed_index": Tool(
        name="fear_greed_index",
        description="Crypto Fear & Greed Index (0=extreme fear, 100=extreme greed) with optional history",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/fear_greed_index",
        price_usdc="0.001",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
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
    ),
    "crypto_news": Tool(
        name="crypto_news",
        description="Latest crypto news and community sentiment from r/CryptoCurrency for any token",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/crypto_news",
        price_usdc="0.003",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
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
    ),
    "defi_tvl": Tool(
        name="defi_tvl",
        description="DeFi protocol Total Value Locked from DeFiLlama. Returns top 10 or a specific protocol.",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/defi_tvl",
        price_usdc="0.002",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
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
    ),
    "whale_activity": Tool(
        name="whale_activity",
        description="Detect recent large wallet movements for a token (whale tracking)",
        endpoint="https://gateway-production-2cc2.up.railway.app/tools/whale_activity",
        price_usdc="0.002",
        developer_address="GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S",
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


def tool_to_dict(tool: Tool) -> dict:
    return asdict(tool)
