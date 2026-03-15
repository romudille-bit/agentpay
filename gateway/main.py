"""
main.py — AgentPay Gateway Server

Run with:
    uvicorn main:app --reload --port 8000

Endpoints:
    GET  /tools                  List all available tools
    GET  /tools/{name}           Get tool details + pricing
    POST /tools/{name}/call      Call a tool (triggers x402 flow)
    POST /tools/register         Register a new tool
    GET  /health                 Health check
    GET  /stats                  Gateway stats
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'registry'))

import logging
import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import asyncio

from x402 import (
    issue_payment_challenge,
    build_402_headers,
    verify_and_fulfill,
    get_pending_count,
)
import registry
from config import settings

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AgentPay Gateway",
    description="x402 payment gateway for MCP tools on Stellar",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Transaction log (in-memory for MVP, use Supabase in prod) ────────────────
_transaction_log: list[dict] = []


# ── Models ────────────────────────────────────────────────────────────────────

class ToolCallRequest(BaseModel):
    parameters: dict = {}
    agent_address: Optional[str] = None  # Agent's Stellar wallet address


class RegisterToolRequest(BaseModel):
    name: str
    description: str
    endpoint: str
    price_usdc: str
    developer_address: str
    parameters: dict
    category: str = "data"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "network": settings.STELLAR_NETWORK,
        "gateway_address": settings.GATEWAY_PUBLIC_KEY or "NOT_CONFIGURED",
        "pending_payments": get_pending_count(),
    }


@app.get("/tools")
async def list_tools(category: Optional[str] = None):
    """List all available tools with pricing."""
    tools = registry.list_tools(category=category)
    return {
        "tools": [registry.tool_to_dict(t) for t in tools],
        "count": len(tools),
    }


@app.get("/tools/{tool_name}")
async def get_tool(tool_name: str):
    """Get details for a specific tool."""
    tool = registry.get_tool(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return registry.tool_to_dict(tool)


@app.post("/tools/{tool_name}/call")
async def call_tool(
    tool_name: str,
    body: ToolCallRequest,
    request: Request,
    x_payment: Optional[str] = Header(None),
    x_agent_address: Optional[str] = Header(None),
):
    """
    Main endpoint — call a paid MCP tool.
    
    Flow:
      1. No X-Payment header → return 402 with challenge
      2. X-Payment present → verify → execute tool → return result
    """
    # Look up tool
    tool = registry.get_tool(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    
    if not tool.active:
        raise HTTPException(status_code=503, detail=f"Tool '{tool_name}' is currently unavailable")

    agent_address = x_agent_address or body.agent_address

    # ── Step 1: No payment header → issue 402 challenge ───────────────────────
    if not x_payment:
        challenge = issue_payment_challenge(
            tool_name=tool_name,
            price_usdc=tool.price_usdc,
            developer_address=tool.developer_address,
            request_data={"parameters": body.parameters},
        )
        
        headers = build_402_headers(challenge)
        return JSONResponse(
            status_code=402,
            content={
                "error": "Payment required",
                "payment_id": challenge.payment_id,
                "amount_usdc": challenge.amount_usdc,
                "pay_to": challenge.gateway_address,
                "asset": "USDC",
                "network": settings.STELLAR_NETWORK,
                "instructions": (
                    f"Send {challenge.amount_usdc} USDC to {challenge.gateway_address} "
                    f"on Stellar {settings.STELLAR_NETWORK} with memo: {challenge.payment_id}. "
                    f"Then retry with header X-Payment: tx_hash=<hash>,from=<your_address>,id={challenge.payment_id}"
                ),
            },
            headers=headers,
        )

    # ── Step 2: Payment header present → verify ───────────────────────────────
    if not agent_address:
        raise HTTPException(
            status_code=400,
            detail="X-Agent-Address header required when providing payment proof"
        )

    auth = await verify_and_fulfill(
        payment_header=x_payment,
        agent_address=agent_address,
    )

    if not auth["authorized"]:
        return JSONResponse(
            status_code=402,
            content={"error": "Payment verification failed", "reason": auth["reason"]},
        )

    # ── Step 3: Payment verified → call the real tool ─────────────────────────
    registry.increment_call_count(tool_name)
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                tool.endpoint,
                json={"parameters": body.parameters},
                headers={"Content-Type": "application/json"},
            )
            if response.status_code != 200:
                raise httpx.ConnectError("Tool server returned non-200")
            tool_result = response.json()
    except httpx.ConnectError:
        logger.warning(f"Tool proxy unavailable for {tool_name}, calling real APIs")
        tool_result = await _real_tool_response(tool_name, body.parameters)
    except Exception as e:
        logger.error(f"Tool execution error: {e}")
        raise HTTPException(status_code=502, detail=f"Tool execution failed: {str(e)}")

    # ── Step 4: Log transaction ───────────────────────────────────────────────
    tx_record = {
        "tool": tool_name,
        "amount_usdc": tool.price_usdc,
        "agent": agent_address,
        "tx_hash": auth.get("tx_hash"),
        "success": True,
    }
    _transaction_log.append(tx_record)
    logger.info(f"Tool call complete: {tool_name} | agent: {agent_address[:8]}...")

    return {
        "tool": tool_name,
        "result": tool_result,
        "payment": {
            "amount_usdc": tool.price_usdc,
            "tx_hash": auth.get("tx_hash"),
            "network": settings.STELLAR_NETWORK,
        },
    }


@app.post("/tools/register")
async def register_tool(body: RegisterToolRequest):
    """Register a new MCP tool in the marketplace."""
    from registry.registry import Tool
    try:
        tool = Tool(
            name=body.name,
            description=body.description,
            endpoint=body.endpoint,
            price_usdc=body.price_usdc,
            developer_address=body.developer_address,
            parameters=body.parameters,
            category=body.category,
        )
        registry.register_tool(tool)
        return {"status": "registered", "tool": registry.tool_to_dict(tool)}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/stats")
async def stats():
    """Gateway statistics."""
    tools = registry.list_tools()
    total_calls = sum(t.total_calls for t in tools)
    return {
        "total_tools": len(tools),
        "total_calls": total_calls,
        "recent_transactions": _transaction_log[-10:],
        "pending_payments": get_pending_count(),
        "network": settings.STELLAR_NETWORK,
    }


# ── Real API implementations ──────────────────────────────────────────────────

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


async def _real_tool_response(tool_name: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            if tool_name == "token_price":
                return await _fetch_token_price(client, params)
            elif tool_name == "wallet_balance":
                return await _fetch_wallet_balance(client, params)
            elif tool_name == "gas_tracker":
                return await _fetch_gas_tracker(client)
            elif tool_name == "dex_liquidity":
                return await _fetch_dex_liquidity(client, params)
            elif tool_name == "whale_activity":
                return await _fetch_whale_activity(client, params)
            else:
                return {"error": f"No real API implementation for tool: {tool_name}"}
        except Exception as e:
            logger.error(f"Real API error for {tool_name}: {e}")
            return {"error": str(e)}


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

    # Fetch recent ERC-20 token balances via token transfer history
    tok_resp = await client.get(base, params={
        "chainid": "1", "module": "account", "action": "tokentx",
        "address": address, "sort": "desc", "page": 1, "offset": 50,
        "apikey": api_key,
    })
    seen_tokens: dict[str, str] = {}
    if tok_resp.status_code == 200:
        for tx in tok_resp.json().get("result", []):
            sym = tx.get("tokenSymbol", "")
            if sym and sym not in seen_tokens:
                seen_tokens[sym] = tx.get("contractAddress", "")

    balances = [{"token": "ETH", "amount": str(round(eth_amount, 6))}]
    for sym in list(seen_tokens)[:8]:
        balances.append({"token": sym, "contract": seen_tokens[sym]})

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
        "pair": f"{token_a.upper()}/{token_b}",
        "price_usd": market.get("current_price", {}).get("usd", 0),
        "volume_24h_usd": market.get("total_volume", {}).get("usd", 0),
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

    for tx in txs:
        try:
            decimals = int(tx.get("tokenDecimal", "18") or "18")
            amount = int(tx.get("value", "0")) / (10 ** decimals)
            usd_value = amount * price_usd if price_usd else None
            if usd_value is not None and usd_value < min_usd:
                continue
            total_volume += usd_value or 0
            large_moves.append({
                "from": tx.get("from", "")[:10] + "...",
                "to": tx.get("to", "")[:10] + "...",
                "amount": round(amount, 4),
                "token": tx.get("tokenSymbol", token),
                "usd_value": round(usd_value, 2) if usd_value else None,
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
        "source": "etherscan",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
