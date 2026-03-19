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
from decimal import Decimal
from fastapi import FastAPI, Request, Response, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional
import asyncio
import textwrap

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


@app.api_route("/tools/{tool_name}", methods=["GET", "HEAD"])
async def get_tool(tool_name: str):
    """Get details for a specific tool."""
    tool = registry.get_tool(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return registry.tool_to_dict(tool)


@app.head("/tools/{tool_name}/call")
async def head_tool(tool_name: str):
    """
    HEAD pre-flight for x402 discovery.
    Returns pricing headers with no body so callers can check cost before committing.
    """
    tool = registry.get_tool(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return Response(
        status_code=200,
        headers={
            "X-Price-USDC":        tool.price_usdc,
            "X-Asset":             "USDC",
            "X-Network":           settings.STELLAR_NETWORK,
            "X-Pay-To":            settings.GATEWAY_PUBLIC_KEY,
            "X-Payment-Required":  "true",
            "X-Tool-Name":         tool_name,
            "X-Tool-Category":     tool.category,
        },
    )


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
        agent_short = (agent_address or "unknown")[:8]
        logger.info(f"[CALL] tool={tool_name} agent={agent_short}... status=402_challenge")
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
    tx_hash = auth.get("tx_hash", "")
    logger.info(f"[CALL] tool={tool_name} agent={agent_address[:8]}... status=completed tx={tx_hash}")

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


GATEWAY_URL = "https://gateway-production-2cc2.up.railway.app"


@app.get("/.well-known/agentpay.json")
async def well_known_agentpay():
    """AgentPay manifest — discoverable by x402-aware agents."""
    tools = registry.list_tools()
    return {
        "name": "AgentPay",
        "version": "1.0",
        "tagline": "Your agent is only as smart as its data",
        "description": "Real-time crypto data for AI agents. Pay per call in USDC on Stellar. No API keys, no subscriptions.",
        "url": GATEWAY_URL,
        "payment_protocol": "x402",
        "payment_network": f"stellar-{settings.STELLAR_NETWORK}",
        "payment_asset": "USDC",
        "pricing_model": "per-call",
        "budget_aware": True,
        "faucet": f"{GATEWAY_URL}/faucet",
        "tools_endpoint": f"{GATEWAY_URL}/tools",
        "capabilities": ["market-data", "onchain-analytics", "defi", "sentiment", "whale-tracking"],
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "price_usdc": t.price_usdc,
                "category": t.category,
                "parameters": t.parameters,
                "endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "triggers": t.triggers,
                "use_when": t.use_when,
                "returns": t.returns,
            }
            for t in tools
        ],
    }


@app.get("/.well-known/agent.json")
async def well_known_agent():
    """A2A protocol card — agent-to-agent discovery."""
    tools = registry.list_tools()
    return {
        "name": "AgentPay Data Gateway",
        "description": "Autonomous crypto data tools for AI agents",
        "url": GATEWAY_URL,
        "version": "1.0",
        "capabilities": {
            "tools": True,
            "payments": "x402/stellar",
            "budget_sessions": True,
        },
        "contact": "https://github.com/romudille-bit/agentpay",
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "price_usdc": t.price_usdc,
                "category": t.category,
                "call_endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "triggers": t.triggers,
                "use_when": t.use_when,
                "returns": t.returns,
            }
            for t in tools
        ],
    }


@app.get("/sitemap.xml", response_class=Response)
async def sitemap():
    tools = registry.list_tools()
    urls = [
        f"{GATEWAY_URL}/",
        f"{GATEWAY_URL}/tools",
        f"{GATEWAY_URL}/.well-known/agentpay.json",
        f"{GATEWAY_URL}/.well-known/agent.json",
        f"{GATEWAY_URL}/faucet/ui",
    ] + [f"{GATEWAY_URL}/tools/{t.name}" for t in tools]

    loc_tags = "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{loc_tags}
</urlset>"""
    return Response(content=xml, media_type="application/xml")


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


# ── Faucet ────────────────────────────────────────────────────────────────────

async def _provision_wallet() -> dict:
    """
    Create and fund a fresh Stellar testnet wallet with XLM + 5 USDC.

    Steps:
      1. Generate keypair
      2. Fund with XLM via Friendbot
      3. Add USDC trustline (signed by new keypair)
      4. Send 1 USDC from gateway wallet (checks balance ≥ 10 USDC first)
      5. Return balances + ready-to-use code snippet
    """
    from stellar_sdk import Keypair, TransactionBuilder
    from stellar import get_server, get_network_passphrase, get_usdc_asset

    server             = get_server()
    network_passphrase = get_network_passphrase()
    usdc               = get_usdc_asset()

    if not settings.GATEWAY_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Gateway wallet not configured")

    # ── 1. Generate keypair ───────────────────────────────────────────────────
    keypair    = Keypair.random()
    public_key = keypair.public_key
    secret_key = keypair.secret

    # ── 2. Fund with XLM via Friendbot ───────────────────────────────────────
    async with httpx.AsyncClient(timeout=30.0) as client:
        fb = await client.get(
            "https://friendbot.stellar.org/",
            params={"addr": public_key},
        )
    if fb.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Friendbot failed: {fb.text[:200]}",
        )

    # ── 3. Add USDC trustline (signed by new wallet) ──────────────────────────
    new_account = server.load_account(public_key)
    trust_tx = (
        TransactionBuilder(
            source_account=new_account,
            network_passphrase=network_passphrase,
            base_fee=100,
        )
        .append_change_trust_op(asset=usdc)
        .set_timeout(30)
        .build()
    )
    trust_tx.sign(keypair)
    server.submit_transaction(trust_tx)

    # ── 4. Send 1 USDC from gateway (with balance guard) ─────────────────────
    gateway_keypair = Keypair.from_secret(settings.GATEWAY_SECRET_KEY)
    from stellar import get_usdc_balance
    gateway_usdc = Decimal(get_usdc_balance(gateway_keypair.public_key))
    if gateway_usdc < Decimal("10"):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Faucet is temporarily empty (balance: {gateway_usdc} USDC). "
                "Please try again later or reach out on GitHub."
            ),
        )
    gateway_account = server.load_account(gateway_keypair.public_key)
    pay_tx = (
        TransactionBuilder(
            source_account=gateway_account,
            network_passphrase=network_passphrase,
            base_fee=100,
        )
        .append_payment_op(
            destination=public_key,
            asset=usdc,
            amount="1",
        )
        .set_timeout(30)
        .build()
    )
    pay_tx.sign(gateway_keypair)
    server.submit_transaction(pay_tx)

    # ── 5. Read balances ──────────────────────────────────────────────────────
    funded = server.load_account(public_key)
    xlm_balance  = "0"
    usdc_balance = "0"
    for b in funded.raw_data.get("balances", []):
        if b.get("asset_type") == "native":
            xlm_balance = b["balance"]
        elif b.get("asset_code") == "USDC":
            usdc_balance = b["balance"]

    # ── 6. Python code snippet ────────────────────────────────────────────────
    gateway_url = "https://gateway-production-2cc2.up.railway.app"
    snippet = textwrap.dedent(f"""\
        from agent.wallet import AgentWallet, Session

        wallet = AgentWallet(
            secret_key="{secret_key}",
            network="testnet",
        )

        GATEWAY = "{gateway_url}"

        with Session(wallet=wallet, gateway_url=GATEWAY, max_spend="0.05") as session:
            r = session.call("token_price", {{"symbol": "ETH"}})
            print(f"ETH: ${{r['result']['price_usd']:,.2f}}")

            r = session.call("gas_tracker", {{}})
            print(f"Gas: {{r['result']['fast_gwei']}} gwei")

            print(f"Spent: {{session.spent()}}  Remaining: {{session.remaining()}}")
    """)

    return {
        "public_key":   public_key,
        "secret_key":   secret_key,
        "usdc_balance": usdc_balance,
        "xlm_balance":  xlm_balance,
        "network":      "testnet",
        "gateway_url":  gateway_url,
        "snippet":      snippet,
        "warning":      "⚠️ Testnet only. Never share your secret key on mainnet. This wallet is for testing AgentPay only.",
    }


@app.get("/faucet")
async def faucet_json():
    """Generate a funded testnet wallet — returns JSON."""
    return await _provision_wallet()


@app.get("/faucet/ui", response_class=HTMLResponse)
async def faucet_ui():
    """Browser-friendly faucet page."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AgentPay Faucet — Get a Test Wallet</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
      background: #0d0d0d; color: #e8e8e8; min-height: 100vh;
      display: flex; flex-direction: column; align-items: center;
      padding: 3rem 1rem;
    }
    h1 { font-size: 2rem; font-weight: 700; margin-bottom: .4rem; }
    .subtitle { color: #888; margin-bottom: 2.5rem; font-size: 1rem; }
    .card {
      background: #181818; border: 1px solid #2a2a2a; border-radius: 12px;
      padding: 2rem; max-width: 640px; width: 100%;
    }
    button {
      width: 100%; padding: 1rem; font-size: 1.1rem; font-weight: 600;
      background: #7c3aed; color: #fff; border: none; border-radius: 8px;
      cursor: pointer; transition: background .2s;
    }
    button:hover:not(:disabled) { background: #6d28d9; }
    button:disabled { background: #3a3a3a; cursor: not-allowed; }
    .spinner {
      display: none; text-align: center; color: #888;
      margin-top: 1.5rem; font-size: .9rem;
    }
    .result { display: none; margin-top: 1.8rem; }
    .field { margin-bottom: 1.2rem; }
    .label { font-size: .75rem; text-transform: uppercase; letter-spacing: .08em;
             color: #888; margin-bottom: .35rem; }
    .value {
      font-family: "SF Mono", "Fira Code", monospace; font-size: .85rem;
      background: #111; border: 1px solid #2a2a2a; border-radius: 6px;
      padding: .6rem .8rem; word-break: break-all; position: relative;
    }
    .balances { display: flex; gap: 1rem; }
    .balance-box {
      flex: 1; background: #111; border: 1px solid #2a2a2a; border-radius: 8px;
      padding: 1rem; text-align: center;
    }
    .balance-amount { font-size: 1.5rem; font-weight: 700; color: #a78bfa; }
    .balance-token  { font-size: .8rem; color: #888; margin-top: .2rem; }
    .snippet-wrap {
      background: #111; border: 1px solid #2a2a2a; border-radius: 6px;
      padding: 1rem; overflow-x: auto;
    }
    pre { font-size: .8rem; line-height: 1.6; color: #c4b5fd; }
    .copy-btn {
      width: auto; padding: .35rem .8rem; font-size: .8rem;
      background: #2a2a2a; border-radius: 4px; margin-top: .5rem;
    }
    .copy-btn:hover { background: #3a3a3a; }
    .warning {
      margin-top: 1.5rem; padding: .75rem 1rem;
      background: #1c1200; border: 1px solid #4a3000; border-radius: 6px;
      font-size: .82rem; color: #f59e0b;
    }
    .error {
      margin-top: 1.5rem; padding: .75rem 1rem;
      background: #1c0000; border: 1px solid #4a0000; border-radius: 6px;
      color: #f87171;
    }
  </style>
</head>
<body>
  <h1>AgentPay Faucet</h1>
  <p class="subtitle">Get a funded Stellar testnet wallet — ready to call paid tools in seconds.</p>

  <div class="card">
    <button id="btn" onclick="getWallet()">Get Test Wallet</button>
    <div class="spinner" id="spinner">
      ⏳ Creating wallet, adding trustline, sending USDC… (~5–10s)
    </div>

    <div class="result" id="result">
      <div class="balances" id="balances"></div>

      <div class="field" style="margin-top:1.2rem">
        <div class="label">Public Key</div>
        <div class="value" id="pub"></div>
      </div>

      <div class="field">
        <div class="label">Secret Key — keep this private!</div>
        <div class="value" id="sec" style="color:#f87171"></div>
      </div>

      <div class="field">
        <div class="label">Ready-to-use Python snippet</div>
        <div class="snippet-wrap"><pre id="snip"></pre></div>
        <button class="copy-btn" onclick="copySnippet()">Copy snippet</button>
      </div>

      <div class="warning">
        ⚠️ Testnet only. Never share your secret key on mainnet. This wallet is for testing AgentPay only.
      </div>
    </div>

    <div class="error" id="error" style="display:none"></div>
  </div>

  <script>
    async function getWallet() {
      const btn     = document.getElementById('btn');
      const spinner = document.getElementById('spinner');
      const result  = document.getElementById('result');
      const errBox  = document.getElementById('error');

      btn.disabled      = true;
      spinner.style.display = 'block';
      result.style.display  = 'none';
      errBox.style.display  = 'none';

      try {
        const res  = await fetch('/faucet');
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

        document.getElementById('pub').textContent  = data.public_key;
        document.getElementById('sec').textContent  = data.secret_key;
        document.getElementById('snip').textContent = data.snippet;

        document.getElementById('balances').innerHTML = `
          <div class="balance-box">
            <div class="balance-amount">${parseFloat(data.usdc_balance).toFixed(2)}</div>
            <div class="balance-token">USDC</div>
          </div>
          <div class="balance-box">
            <div class="balance-amount">${parseFloat(data.xlm_balance).toFixed(2)}</div>
            <div class="balance-token">XLM (gas)</div>
          </div>
        `;

        result.style.display = 'block';
      } catch (e) {
        errBox.textContent   = '❌ ' + e.message;
        errBox.style.display = 'block';
        btn.disabled = false;
      } finally {
        spinner.style.display = 'none';
      }
    }

    function copySnippet() {
      navigator.clipboard.writeText(document.getElementById('snip').textContent);
    }
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


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
            elif tool_name == "dune_query":
                return await _fetch_dune_query(client, params)
            elif tool_name == "fear_greed_index":
                return await _fetch_fear_greed_index(client, params)
            elif tool_name == "crypto_news":
                return await _fetch_crypto_news(client, params)
            elif tool_name == "defi_tvl":
                return await _fetch_defi_tvl(client, params)
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
            if price_usd:
                usd_value = amount * price_usd
                if usd_value < min_usd:
                    continue  # reliably below threshold — skip
            else:
                usd_value = None  # price unavailable — include with null
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


async def _fetch_dune_query(client: httpx.AsyncClient, params: dict) -> dict:
    if not settings.DUNE_API_KEY:
        return {"error": "DUNE_API_KEY not configured"}

    query_id = params.get("query_id")
    if not query_id:
        return {"error": "query_id is required"}

    query_parameters = params.get("query_parameters", {})
    limit = int(params.get("limit", 25))
    headers = {"X-Dune-API-Key": settings.DUNE_API_KEY}
    dune_base = "https://api.dune.com/api/v1"

    # Fast-path: try cached results first
    if not query_parameters:
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
                }

    # Execute fresh query
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
