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
import time as _time
import httpx
from decimal import Decimal
from fastapi import FastAPI, Request, Response, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional
import asyncio
import textwrap
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from x402 import (
    issue_payment_challenge,
    build_402_headers,
    verify_and_fulfill,
    get_pending_count,
)
import registry
from registry import reload_tools
from config import settings
import base as base_pay

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AgentPay Gateway",
    description="x402 payment gateway for MCP tools on Stellar",
    version="0.1.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Faucet abuse-prevention ───────────────────────────────────────────────────
# Maps IP → epoch timestamp of last successful faucet request.
# Requests within 24 hours of a prior grant are rejected.
_FAUCET_IP_LOG: dict[str, float] = {}
_FAUCET_COOLDOWN_SECS = 86_400  # 24 hours

# ── In-memory response cache ──────────────────────────────────────────────────
# key → (expires_at_monotonic, data)
_cache: dict[str, tuple[float, dict]] = {}

_CACHE_TTL: dict[str, int] = {
    "token_price":      60,   # 60 seconds
    "gas_tracker":      30,   # 30 seconds
    "fear_greed_index": 300,  # 5 minutes
    "defi_tvl":         300,  # 5 minutes
}


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry and _time.monotonic() < entry[0]:
        return entry[1]
    return None


def _cache_set(key: str, value: dict, ttl: int) -> None:
    _cache[key] = (_time.monotonic() + ttl, value)


# ── Supabase helpers (raw httpx — works with sb_publishable_ key format) ──────

def _sb_headers() -> dict:
    """Headers for Supabase REST API calls."""
    return {
        "apikey":        settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


def _sb_enabled() -> bool:
    return bool(settings.SUPABASE_URL and settings.SUPABASE_KEY)


async def _keepalive_loop():
    """Ping /health every 5 minutes to prevent Railway cold-start."""
    await asyncio.sleep(60)  # wait for full startup before first ping
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{GATEWAY_URL}/health")
        except Exception:
            pass  # silent — keepalive is best-effort
        await asyncio.sleep(300)  # 5 minutes


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_keepalive_loop())
    if not _sb_enabled():
        logger.info("Supabase not configured — using in-memory registry")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/tools",
                headers=_sb_headers(),
                params={"select": "*", "active": "eq.true"},
            )
        if resp.status_code != 200:
            logger.warning(f"Supabase tools fetch failed ({resp.status_code}) — using in-memory registry")
            return
        rows = resp.json()
        if not rows:
            logger.warning("Supabase tools table empty — using in-memory registry")
            return
        from registry import Tool
        tools = [
            Tool(
                name=r["name"],
                description=r.get("description", ""),
                endpoint=r.get("endpoint", ""),
                price_usdc=r.get("price_usdc", "0"),
                developer_address=r.get("developer_address", ""),
                parameters=r.get("parameters", {}),
                category=r.get("category", "data"),
                active=r.get("active", True),
                uptime_pct=r.get("uptime_pct", 100.0),
                total_calls=r.get("total_calls", 0),
                triggers=r.get("triggers", []),
                use_when=r.get("use_when", ""),
                returns=r.get("returns", ""),
            )
            for r in rows
        ]
        reload_tools(tools)
        logger.info(f"Loaded {len(tools)} tools from Supabase")
    except Exception as e:
        logger.warning(f"Supabase unavailable ({e}) — using in-memory registry")


async def _log_payment(
    payment_id: str,
    tool_name: str,
    agent_address: str,
    amount_usdc: str,
    tx_hash: str,
) -> None:
    """Fire-and-forget: log a completed payment to Supabase."""
    if not _sb_enabled():
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers=_sb_headers(),
                json={
                    "payment_id":    payment_id,
                    "tool_name":     tool_name,
                    "agent_address": agent_address,
                    "amount_usdc":   amount_usdc,
                    "tx_hash":       tx_hash,
                    "status":        "completed",
                },
            )
    except Exception as e:
        logger.warning(f"Payment log to Supabase failed: {e}")

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


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {
        "name":             "AgentPay",
        "tagline":          "Your agent is only as smart as its data",
        "version":          "1.0",
        "tools":            12,
        "docs":             "https://github.com/romudille-bit/agentpay",
        "tools_endpoint":   "https://gateway-production-2cc2.up.railway.app/tools",
        "faucet":           "https://gateway-production-2cc2.up.railway.app/faucet",
        "discovery":        "https://gateway-production-2cc2.up.railway.app/.well-known/agentpay.json",
        "payment_networks": ["stellar", "base"],
    }


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
@limiter.limit("100/minute")
async def call_tool(
    tool_name: str,
    body: ToolCallRequest,
    request: Request,
    x_payment: Optional[str] = Header(None),
    x_agent_address: Optional[str] = Header(None),
    payment_signature: Optional[str] = Header(None),   # x402 v2 Base/EVM
):
    """
    Main endpoint — call a paid MCP tool.

    Supports two payment paths:
      Stellar — X-Payment: tx_hash=<hash>,from=<addr>,id=<payment_id>
      Base    — PAYMENT-SIGNATURE: <base64(PaymentPayload JSON)>

    Flow:
      1. Neither header → return 402 advertising both options
      2. X-Payment → verify Stellar tx, execute tool
      3. PAYMENT-SIGNATURE → settle via CDP facilitator, execute tool
    """
    tool = registry.get_tool(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    if not tool.active:
        raise HTTPException(status_code=503, detail=f"Tool '{tool_name}' is currently unavailable")

    agent_address = x_agent_address or body.agent_address
    resource_url  = f"{GATEWAY_URL}/tools/{tool_name}/call"

    # ── Step 1: No payment → 402 with both Stellar + Base options ────────────
    if not x_payment and not payment_signature:
        agent_short = (agent_address or "unknown")[:8]
        logger.info(f"[CALL] tool={tool_name} agent={agent_short}... status=402_challenge")

        challenge = issue_payment_challenge(
            tool_name=tool_name,
            price_usdc=tool.price_usdc,
            developer_address=tool.developer_address,
            request_data={"parameters": body.parameters},
        )

        # Build Base payment requirements (if configured)
        base_option = None
        payment_required_header = None
        if settings.BASE_GATEWAY_ADDRESS:
            base_req = base_pay.build_payment_requirements(
                amount_usdc=tool.price_usdc,
                pay_to=settings.BASE_GATEWAY_ADDRESS,
                resource_url=resource_url,
                network=settings.BASE_NETWORK,
            )
            base_option = {
                "scheme":            base_req["scheme"],
                "network":           base_req["network"],
                "amount_atomic":     base_req["amount"],
                "amount_usdc":       tool.price_usdc,
                "asset":             base_req["asset"],
                "pay_to":            settings.BASE_GATEWAY_ADDRESS,
                "maxTimeoutSeconds": base_req["maxTimeoutSeconds"],
                "instructions": (
                    "Sign an EIP-3009 transferWithAuthorization for the amount above, "
                    "encode as base64 JSON PaymentPayload, and retry with header "
                    "PAYMENT-SIGNATURE: <base64_payload>"
                ),
            }
            payment_required_header = base_pay.build_payment_required_header(
                requirements=base_req,
                resource_url=resource_url,
                tool_description=tool.description,
            )

        headers = build_402_headers(challenge)
        if payment_required_header:
            headers["PAYMENT-REQUIRED"] = payment_required_header

        body_content = {
            "error":       "Payment required",
            "x402Version": 2,
            # ── Stellar option (backward-compatible) ──────────────────────────
            "payment_id":  challenge.payment_id,
            "amount_usdc": challenge.amount_usdc,
            "pay_to":      challenge.gateway_address,
            "asset":       "USDC",
            "network":     settings.STELLAR_NETWORK,
            "instructions": (
                f"[Stellar] Send {challenge.amount_usdc} USDC to {challenge.gateway_address} "
                f"on Stellar {settings.STELLAR_NETWORK} with memo: {challenge.payment_id}. "
                f"Retry with X-Payment: tx_hash=<hash>,from=<addr>,id={challenge.payment_id}. "
                f"No Stellar wallet? Get a free funded testnet wallet instantly: {GATEWAY_URL}/faucet"
            ),
            # ── Structured options for multi-chain clients ────────────────────
            "payment_options": {
                "stellar": {
                    "payment_id":  challenge.payment_id,
                    "amount_usdc": challenge.amount_usdc,
                    "pay_to":      challenge.gateway_address,
                    "network":     settings.STELLAR_NETWORK,
                    "asset":       "USDC",
                    "header":      f"X-Payment: tx_hash=<hash>,from=<addr>,id={challenge.payment_id}",
                },
                **({"base": base_option} if base_option else {}),
            },
        }

        return JSONResponse(status_code=402, content=body_content, headers=headers)

    # ── Step 2a: Stellar payment ───────────────────────────────────────────────
    if x_payment:
        if not agent_address:
            raise HTTPException(status_code=400, detail="agent_address required (body or X-Agent-Address header)")

        auth = await verify_and_fulfill(payment_header=x_payment, agent_address=agent_address)
        if not auth["authorized"]:
            return JSONResponse(
                status_code=402,
                content={"error": "Payment verification failed", "reason": auth["reason"]},
            )

    # ── Step 2b: Base/EVM payment (Mode A: CDP facilitator, Mode B: on-chain tx) ─
    elif payment_signature:
        if not settings.BASE_GATEWAY_ADDRESS:
            raise HTTPException(status_code=503, detail="Base payment not configured on this gateway")

        base_req = base_pay.build_payment_requirements(
            amount_usdc=tool.price_usdc,
            pay_to=settings.BASE_GATEWAY_ADDRESS,
            resource_url=resource_url,
            network=settings.BASE_NETWORK,
        )
        result = await base_pay.settle_base_payment(
            payment_signature, base_req, rpc_url=settings.BASE_RPC_URL
        )
        if not result["success"]:
            return JSONResponse(
                status_code=402,
                content={"error": "Base payment settlement failed", "reason": result["reason"]},
            )
        logger.info(f"[CALL] tool={tool_name} agent={result['payer'][:8]}... status=base_settled tx={result['tx_hash'][:16]}")
        auth = {
            "authorized": True,
            "tx_hash":    result["tx_hash"],
            "payer":      result["payer"],
            "network":    result["network"],
        }
        # Use EVM payer address as agent_address for logging
        agent_address = agent_address or result["payer"]

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
    tx_hash    = auth.get("tx_hash", "")
    agent_log  = (agent_address or "unknown")[:8]
    logger.info(f"[CALL] tool={tool_name} agent={agent_log}... status=completed tx={tx_hash}")

    # Log to Supabase (non-blocking — failure is silently warned, not fatal)
    if x_payment and "id=" in x_payment:
        payment_id = x_payment.split("id=")[-1]
    else:
        payment_id = tx_hash  # Base: use tx hash as dedup key
    asyncio.create_task(_log_payment(
        payment_id=payment_id,
        tool_name=tool_name,
        agent_address=agent_address or "",
        amount_usdc=tool.price_usdc,
        tx_hash=tx_hash,
    ))

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
    from registry import Tool
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
                "response_example": t.response_example,
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


@app.get("/.well-known/l402-services")
async def well_known_l402_services():
    """402index.io discovery document — lists all paid endpoints with pricing and request schemas."""
    tools = registry.list_tools()

    def _request_body(tool) -> dict:
        """Convert JSON-Schema parameters to 402index request_body format."""
        props = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        return {
            field: {
                **spec,
                "required": field in required,
            }
            for field, spec in props.items()
        }

    return {
        "version": "0.2.0",
        "name": "AgentPay",
        "description": "Real-time crypto data for AI agents. Pay per call in USDC on Stellar or Base. No API keys.",
        "homepage": GATEWAY_URL,
        "protocol": "x402",
        "protocols": ["x402"],
        "payment_network": "stellar",
        "services": [
            {
                "id": t.name,
                "name": t.name.replace("_", " ").title(),
                "description": t.description,
                "endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "method": "POST",
                "content_type": "application/json",
                "pricing": {
                    "amount": float(t.price_usdc),
                    "currency": "USD",
                    "type": "fixed",
                },
                "request_body": _request_body(t),
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

async def _provision_wallet(base_url: str) -> dict:
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
    logger.info(f"[FAUCET] step=1/5 generated keypair {public_key[:8]}...")

    # ── 2. Fund with XLM via Friendbot ───────────────────────────────────────
    logger.info(f"[FAUCET] step=2/5 calling Friendbot")
    async with httpx.AsyncClient(timeout=60.0) as client:
        fb = await client.get(
            "https://friendbot.stellar.org/",
            params={"addr": public_key},
        )
    if fb.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Friendbot failed: {fb.text[:200]}",
        )
    logger.info(f"[FAUCET] step=2/5 Friendbot OK")

    # ── 3. Add USDC trustline (signed by new wallet) ──────────────────────────
    logger.info(f"[FAUCET] step=3/5 adding USDC trustline")
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
    logger.info(f"[FAUCET] step=3/5 trustline submitted")

    # ── 4. Send 1 USDC from gateway (with balance guard) ─────────────────────
    logger.info(f"[FAUCET] step=4/5 checking gateway balance and sending 0.05 USDC")
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
            amount="0.05",
        )
        .set_timeout(30)
        .build()
    )
    pay_tx.sign(gateway_keypair)
    server.submit_transaction(pay_tx)
    logger.info(f"[FAUCET] step=4/5 USDC sent")

    # ── 5. Read balances ──────────────────────────────────────────────────────
    logger.info(f"[FAUCET] step=5/5 reading final balances")
    funded = server.load_account(public_key)
    xlm_balance  = "0"
    usdc_balance = "0"
    for b in funded.raw_data.get("balances", []):
        if b.get("asset_type") == "native":
            xlm_balance = b["balance"]
        elif b.get("asset_code") == "USDC":
            usdc_balance = b["balance"]

    # ── 6. Python code snippet ────────────────────────────────────────────────
    gateway_url = base_url
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

    logger.info(f"[FAUCET] done — wallet {public_key[:8]}... usdc={usdc_balance} xlm={xlm_balance}")
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
@limiter.limit("2/hour")
async def faucet_json(request: Request):
    """Generate a funded testnet wallet — returns JSON."""
    # ── IP cooldown: one wallet per IP per 24 hours ───────────────────────────
    client_ip = request.client.host if request.client else "unknown"
    now = _time.time()
    last = _FAUCET_IP_LOG.get(client_ip, 0)
    if now - last < _FAUCET_COOLDOWN_SECS:
        wait_h = int((_FAUCET_COOLDOWN_SECS - (now - last)) / 3600) + 1
        raise HTTPException(
            status_code=429,
            detail=f"This IP already received a test wallet. Try again in ~{wait_h}h.",
        )

    # ── Anti-script delay (3 seconds) ─────────────────────────────────────────
    await asyncio.sleep(3)

    base_url = settings.AGENTPAY_GATEWAY_URL or GATEWAY_URL
    result = await _provision_wallet(base_url)

    # Record IP only after successful wallet creation
    _FAUCET_IP_LOG[client_ip] = _time.time()
    return result


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
    # Build cache key (include params for tools where they matter)
    if tool_name in _CACHE_TTL:
        cache_key = f"{tool_name}:{sorted(params.items())}"
        cached = _cache_get(cache_key)
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
            elif tool_name == "dex_liquidity":
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
            else:
                result = {"error": f"No real API implementation for tool: {tool_name}"}
        except Exception as e:
            logger.error(f"Real API error for {tool_name}: {e}")
            result = {"error": str(e)}

    if cache_key and "error" not in result:
        _cache_set(cache_key, result, _CACHE_TTL[tool_name])

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
