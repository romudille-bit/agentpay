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
        endpoint="http://localhost:8001/tools/token_price",
        price_usdc="0.001",
        developer_address="PLACEHOLDER_DEV_WALLET_1",  # Replace with real address
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
        endpoint="http://localhost:8001/tools/wallet_balance",
        price_usdc="0.002",
        developer_address="PLACEHOLDER_DEV_WALLET_1",
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
        endpoint="http://localhost:8001/tools/dex_liquidity",
        price_usdc="0.003",
        developer_address="PLACEHOLDER_DEV_WALLET_2",
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
        endpoint="http://localhost:8001/tools/gas_tracker",
        price_usdc="0.001",
        developer_address="PLACEHOLDER_DEV_WALLET_2",
        parameters={
            "type": "object",
            "properties": {},
        },
        category="data",
    ),
    "dune_query": Tool(
        name="dune_query",
        description="Run any Dune Analytics query and return live onchain results by query ID",
        endpoint="http://localhost:8002/",
        price_usdc="0.005",
        developer_address="PLACEHOLDER_DEV_WALLET_1",
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
    "whale_activity": Tool(
        name="whale_activity",
        description="Detect recent large wallet movements for a token (whale tracking)",
        endpoint="http://localhost:8001/tools/whale_activity",
        price_usdc="0.002",
        developer_address="PLACEHOLDER_DEV_WALLET_3",
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
