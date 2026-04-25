"""
routes/tools.py — Tool listing, lookup, payment, and registration endpoints.

  GET  /tools                — list all tools (optional category filter)
  GET  /tools/{name}         — single tool details (alias-aware)
  HEAD /tools/{name}         — same as GET (alias-aware)
  HEAD /tools/{name}/call    — x402 pre-flight (advertise pricing + networks)
  POST /tools/{name}/call    — full x402 flow: 402 → pay → execute
  POST /tools/register       — register a new tool

The POST /tools/{name}/call handler is the heart of the gateway. Three
states:
  1. No payment header        → return 402 with Stellar + Base options.
  2. X-Payment header         → verify Stellar tx, execute tool.
  3. PAYMENT-SIGNATURE header → settle Base via CDP/JSON-RPC, execute tool.
"""

import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import registry

from gateway import base as base_pay
from gateway._limiter import limiter
from gateway.config import GATEWAY_URL, settings
from gateway.services.supabase import log_payment
from gateway.services.tools_runtime import real_tool_response
from gateway.services.transaction_log import append_transaction
from gateway.x402 import (
    build_402_headers,
    issue_payment_challenge,
    parse_payment_header,
    verify_and_fulfill,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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


# Legacy alias map: client called the old name → resolve to current canonical.
_TOOL_ALIASES = {
    "dex_liquidity": "token_market_data",
}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/tools")
async def list_tools(category: Optional[str] = None):
    """List all available tools with pricing."""
    tools = registry.list_tools(category=category)
    return {
        "tools": [registry.tool_to_dict(t) for t in tools],
        "count": len(tools),
    }


@router.api_route("/tools/{tool_name}", methods=["GET", "HEAD"])
async def get_tool(tool_name: str):
    """Get details for a specific tool. Supports legacy aliases."""
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = registry.get_tool(resolved)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return registry.tool_to_dict(tool)


@router.head("/tools/{tool_name}/call")
async def head_tool(tool_name: str):
    """
    HEAD pre-flight for x402 discovery.
    Returns pricing headers with no body so callers can check cost before committing.
    Also advertises the Base/EVM payment option when BASE_GATEWAY_ADDRESS is set.
    """
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = registry.get_tool(resolved)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    headers = {
        "X-Price-USDC":        tool.price_usdc,
        "X-Asset":             "USDC",
        "X-Network":           f"stellar-{settings.STELLAR_NETWORK}",
        "X-Pay-To":            settings.GATEWAY_PUBLIC_KEY,
        "X-Payment-Required":  "true",
        "X-Tool-Name":         tool_name,
        "X-Tool-Category":     tool.category,
    }
    if settings.BASE_GATEWAY_ADDRESS:
        headers["X-Base-Network"] = settings.BASE_NETWORK
        headers["X-Base-Pay-To"]  = settings.BASE_GATEWAY_ADDRESS
    return Response(status_code=200, headers=headers)


@router.post("/tools/{tool_name}/call")
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
    # Resolve legacy aliases (e.g. dex_liquidity → token_market_data) so POST
    # honours the same alias map as GET /tools/{name}. Without this, agents
    # calling the legacy name hit a 404 even though the alias resolver works
    # for tool metadata.
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = registry.get_tool(resolved)
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

        agent_short = (agent_address or "unknown")[:8]
        logger.info(f"[PAYMENT] tool={tool_name} network=stellar agent={agent_short}... verifying X-Payment header")
        auth = await verify_and_fulfill(payment_header=x_payment, agent_address=agent_address)
        if not auth["authorized"]:
            status = "REPLAY_ATTACK" if "replay" in auth["reason"].lower() else "FAILED"
            logger.info(f"[PAYMENT] tool={tool_name} network=stellar agent={agent_short}... status={status} reason={auth['reason']}")
            return JSONResponse(
                status_code=402,
                content={"error": "Payment verification failed", "reason": auth["reason"]},
            )
        logger.info(f"[PAYMENT] tool={tool_name} network=stellar agent={agent_short}... status=OK tx={auth.get('tx_hash','')[:16]}")

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
        logger.info(f"[PAYMENT] tool={tool_name} network=base verifying PAYMENT-SIGNATURE header")
        result = await base_pay.settle_base_payment(
            payment_signature, base_req, rpc_url=settings.BASE_RPC_URL
        )
        if not result["success"]:
            status = "REPLAY_ATTACK" if result["reason"] == "replay_attack" else "FAILED"
            logger.info(f"[PAYMENT] tool={tool_name} network=base status={status} reason={result['reason']}")
            return JSONResponse(
                status_code=402,
                content={"error": "Base payment settlement failed", "reason": result["reason"]},
            )
        logger.info(f"[PAYMENT] tool={tool_name} network=base agent={result['payer'][:8]}... status=OK tx={result['tx_hash'][:16]}")
        auth = {
            "authorized": True,
            "tx_hash":    result["tx_hash"],
            "payer":      result["payer"],
            "network":    result["network"],
        }
        # Use EVM payer address as agent_address for logging
        agent_address = agent_address or result["payer"]

    # ── Step 3: Payment verified → call the real tool ─────────────────────────
    # Use the resolved name for registry book-keeping so legacy aliases
    # (e.g. dex_liquidity) credit the canonical tool. Pass `resolved` to the
    # real-tool dispatcher too — real_tool_response handles both names but
    # using the canonical one keeps the cache key and metrics consistent.
    registry.increment_call_count(resolved)

    if not tool.endpoint:
        # No proxy endpoint configured — call real APIs directly
        tool_result = await real_tool_response(resolved, body.parameters)
    else:
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
            tool_result = await real_tool_response(resolved, body.parameters)
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
    append_transaction(tx_record)
    tx_hash    = auth.get("tx_hash", "")
    agent_log  = (agent_address or "unknown")[:8]
    logger.info(f"[CALL] tool={tool_name} agent={agent_log}... status=completed tx={tx_hash}")

    # Log to Supabase (non-blocking — failure is silently warned, not fatal).
    # Use the proper x402 header parser instead of fragile string-split, which
    # would capture the wrong substring whenever id= wasn't the final field.
    if x_payment:
        parsed = parse_payment_header(x_payment) or {}
        payment_id = parsed.get("id") or tx_hash
    else:
        payment_id = tx_hash  # Base: use tx hash as dedup key
    asyncio.create_task(log_payment(
        payment_id=payment_id,
        tool_name=resolved,
        agent_address=agent_address or "",
        amount_usdc=tool.price_usdc,
        tx_hash=tx_hash,
    ))

    # Report the actual settlement network — Base payments were previously
    # mislabelled as STELLAR_NETWORK in the receipt.
    receipt_network = auth.get("network") or f"stellar-{settings.STELLAR_NETWORK}"

    return {
        "tool": tool_name,
        "result": tool_result,
        "payment": {
            "amount_usdc": tool.price_usdc,
            "tx_hash": auth.get("tx_hash"),
            "network": receipt_network,
        },
    }


@router.post("/tools/register")
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
