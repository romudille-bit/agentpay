"""
routes/tools.py — Tool listing, lookup, payment, and registration endpoints.

  GET  /tools                — list all tools (optional category filter)
  GET  /tools/{name}         — single tool details (alias-aware)
  HEAD /tools/{name}         — same as GET (alias-aware)
  HEAD /tools/{name}/call    — x402 pre-flight (advertise pricing + networks)
  POST /tools/{name}/call    — full x402 flow: 402 → pay → execute
  POST /tools/register       — register a new tool

POST /tools/{name}/call is the heart of the gateway. call_tool() orchestrates
four stages, each its own function:
  _issue_402         — no payment header → 402 with Stellar + Base options
  _settle_stellar    — X-Payment header → verify Stellar tx
  _settle_base_path  — PAYMENT-SIGNATURE header → settle Base (CDP / JSON-RPC)
  _execute_and_log   — run the tool, write the payment lifecycle, build response
"""

import asyncio
import logging
from typing import Optional, Union

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import registry

from decimal import Decimal

from gateway import base as base_pay
from gateway._limiter import limiter, wallet_or_ip
from gateway.config import GATEWAY_URL, settings
from gateway.services.supabase import (
    insert_pending_payment_log,
    sb_enabled,
    update_payment_log_state,
)
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


# ── Bazaar discovery metadata, per paid tool ──────────────────────────────────
# Bazaar's validation crawl reads extensions.bazaar + resource.serviceName/tags
# from the LIVE 402, and indexing fires on a Mode A settle that carries the
# extension. Tools listed here get both injected (mirrors routes/session.py).
_TOOL_BAZAAR: dict[str, dict] = {
    "pre_trade_check": {
        "resource": {
            "serviceName": "AgentPay",
            "description": (
                "One-call pre-trade sanity check for AI agents: live orderbook "
                "slippage at YOUR size, side-aware funding carry, open-interest "
                "crowding, and optional contract security — composed into a single "
                "ok/caution/avoid verdict with per-factor reasons and raw data "
                "embedded. Replaces four API integrations plus the judgment layer."
            ),
            # ≤5 tags, ≤32 chars each — own the trade-decision category.
            "tags": ["pre-trade-check", "trade-risk", "slippage", "funding-rates", "agent-trading"],
        },
        "extension": {
            "description": (
                "Pre-trade sanity check: 'I want to long $X of SYMBOL — is now "
                "sane?' Live slippage at your size + side-aware funding carry + "
                "OI crowding + optional security scan → one ok/caution/avoid "
                "verdict with per-factor breakdown and raw components embedded."
            ),
            "info": {
                "input": {
                    "type":     "http",
                    "method":   "POST",
                    "bodyType": "json",
                    "body": {
                        "parameters": {
                            "symbol":   "ETH",
                            "size_usd": 50000,
                            "side":     "long",
                        },
                    },
                },
                "output": {
                    "type": "json",
                    "example": {
                        "symbol": "ETH", "side": "long", "size_usd": 50000,
                        "verdict": "ok",
                        "factors": {
                            "liquidity": {"level": "ok", "slippage_pct": 0.001,
                                          "reason": "fills within 0.001% of best price"},
                            "carry":     {"level": "ok", "median_funding_pct": 0.01,
                                          "reason": "carry unremarkable"},
                            "crowding":  {"level": "ok", "long_short_ratio": 1.2,
                                          "reason": "positioning unremarkable"},
                            "security":  {"level": "skipped",
                                          "reason": "no token_address provided"},
                        },
                    },
                },
            },
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "input": {
                        "type": "object",
                        "properties": {
                            "symbol":        {"type": "string"},
                            "size_usd":      {"type": "number"},
                            "side":          {"type": "string", "enum": ["long", "short"]},
                            "token_address": {"type": "string"},
                        },
                        "required": ["symbol"],
                    },
                    "output": {
                        "type": "object",
                        "properties": {
                            "verdict": {"type": "string", "enum": ["ok", "caution", "avoid"]},
                            "factors": {"type": "object"},
                        },
                    },
                },
            },
        },
    },
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


# ── Payment-flow stages (orchestrated by call_tool) ──────────────────────────

def _base_402_option(tool, resource_url: str):
    """Build the Base payment option + PAYMENT-REQUIRED header for a 402.

    Returns (base_option, payment_required_header), both None when
    BASE_GATEWAY_ADDRESS isn't configured.
    """
    if not settings.BASE_GATEWAY_ADDRESS:
        return None, None

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
    # outputSchema feeds Bazaar auto-indexing via the PAYMENT-REQUIRED
    # header; without it the listing has price but no shape.
    output_schema = None
    if tool.parameters or tool.response_example is not None:
        output_schema = {
            "input":  tool.parameters or {},
            "output": tool.response_example,
        }
    bz = _TOOL_BAZAAR.get(tool.name, {})
    payment_required_header = base_pay.build_payment_required_header(
        requirements=base_req,
        resource_url=resource_url,
        tool_description=tool.description,
        output_schema=output_schema,
        bazaar_resource=bz.get("resource"),
        bazaar_extension=bz.get("extension"),
    )
    return base_option, payment_required_header


async def _refund_and_502(tool_name: str, payment_id: str, exc: Exception) -> JSONResponse:
    """Payment accepted on-chain but tool execution failed → refund_pending.

    The PATCH is awaited (terminal state); the background refund worker
    picks the row up when REFUND_ENABLED. The 502 body carries
    payment_status so SDK callers can branch (RefundPending exception).
    """
    logger.error(f"Tool execution error: {exc}")
    await update_payment_log_state(
        payment_id,
        "refund_pending",
        error_reason=f"tool_exec_failed: {str(exc)[:200]}",
    )
    return JSONResponse(
        status_code=502,
        content={
            "error":               "Tool execution failed",
            "tool":                tool_name,
            "payment_id":          payment_id,
            "payment_status":      "refund_pending" if settings.REFUND_ENABLED else "refund_disabled",
            "refund_eta_seconds":  60 if settings.REFUND_ENABLED else None,
            "error_reason":        f"tool_exec_failed: {str(exc)[:200]}",
        },
    )


async def _run_tool(tool, resolved: str, tool_name: str, parameters: dict):
    """Execute the tool — proxy endpoint when configured, real APIs otherwise."""
    if not tool.endpoint:
        return await real_tool_response(resolved, parameters)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                tool.endpoint,
                json={"parameters": parameters},
                headers={"Content-Type": "application/json"},
            )
            if response.status_code != 200:
                raise httpx.ConnectError("Tool server returned non-200")
            return response.json()
    except httpx.ConnectError:
        logger.warning(f"Tool proxy unavailable for {tool_name}, calling real APIs")
        return await real_tool_response(resolved, parameters)


async def _issue_402(
    tool, resolved: str, tool_name: str, body: ToolCallRequest,
    request: Request, agent_address: Optional[str], resource_url: str,
) -> JSONResponse:
    """No payment header → issue a 402 challenge with Stellar + Base options.

    Free tools (price_usdc == 0) issue a 402 too — they flow through the same
    lifecycle as $0 payments so every call gets a payment_logs row and a
    receipt. The SDK skips on-chain settlement for $0 and verify_and_fulfill
    authorizes $0 challenges without requiring a tx.
    """
    agent_short = (agent_address or "unknown")[:8]
    logger.info(f"[CALL] tool={tool_name} agent={agent_short}... status=402_challenge")

    challenge = issue_payment_challenge(
        tool_name=tool_name,
        price_usdc=tool.price_usdc,
        developer_address=tool.developer_address,
        request_data={"parameters": body.parameters},
    )

    # Pre-402 payment_logs INSERT — awaited and fail-closed: the gateway
    # refuses to issue challenges it cannot track. Network defaults to
    # Stellar on this UUID-keyed row; a Base settlement later produces a
    # second row keyed on tx_hash (x402-v2 doesn't carry the UUID through
    # PAYMENT-SIGNATURE) and this one gets swept to 'abandoned'.
    if sb_enabled():
        client_ip  = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        row_id = await insert_pending_payment_log(
            payment_id=challenge.payment_id,
            tool_name=resolved,
            network=f"stellar-{settings.STELLAR_NETWORK}",
            amount_usdc=tool.price_usdc,
            developer_address=tool.developer_address or None,
            client_ip=client_ip,
            user_agent=user_agent,
        )
        if row_id is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Supabase write failure — challenge issuance refused. "
                    "The gateway will not issue 402 challenges it cannot persist. "
                    "Retry shortly."
                ),
            )

    base_option, payment_required_header = _base_402_option(tool, resource_url)

    headers = build_402_headers(challenge)
    if payment_required_header:
        headers["PAYMENT-REQUIRED"] = payment_required_header

    body_content = {
        "error":       "Payment required",
        "x402Version": 2,
        # Stellar option (backward-compatible top-level fields)
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
        # Structured options for multi-chain clients
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


async def _settle_stellar(
    tool_name: str, x_payment: str, agent_address: Optional[str],
) -> Union[dict, JSONResponse]:
    """X-Payment header → verify the Stellar payment.

    Returns the auth dict on success, or a JSONResponse (402) on rejection.
    """
    if not agent_address:
        raise HTTPException(status_code=400, detail="agent_address required (body or X-Agent-Address header)")

    agent_short = (agent_address or "unknown")[:8]
    logger.info(f"[PAYMENT] tool={tool_name} network=stellar agent={agent_short}... verifying X-Payment header")
    auth = await verify_and_fulfill(payment_header=x_payment, agent_address=agent_address)
    if not auth["authorized"]:
        status = "REPLAY_ATTACK" if "replay" in auth["reason"].lower() else "FAILED"
        logger.info(f"[PAYMENT] tool={tool_name} network=stellar agent={agent_short}... status={status} reason={auth['reason']}")

        # Terminal states are AWAITED so analytics are consistent at response
        # time. (A create_task here loses the race: there's no downstream
        # await before the return.) If the header was malformed there's no
        # UUID to PATCH; the pending row becomes 'abandoned' via the sweep.
        rejected_pid = (parse_payment_header(x_payment) or {}).get("id")
        if rejected_pid:
            await update_payment_log_state(
                rejected_pid, "rejected", error_reason=auth["reason"],
            )

        return JSONResponse(
            status_code=402,
            content={"error": "Payment verification failed", "reason": auth["reason"]},
        )
    logger.info(f"[PAYMENT] tool={tool_name} network=stellar agent={agent_short}... status=OK tx={auth.get('tx_hash','')[:16]}")

    # Intermediate 'verified' PATCH — fire-and-forget. expected_state='pending'
    # guards against this landing AFTER the awaited terminal payment_done
    # PATCH (without it, rows stuck at 'verified' in production).
    verified_pid = (parse_payment_header(x_payment) or {}).get("id")
    if verified_pid:
        asyncio.create_task(update_payment_log_state(
            verified_pid, "verified",
            expected_state="pending",
            agent_address=agent_address,
            tx_hash=auth.get("tx_hash"),
        ))
    return auth


async def _settle_base_path(
    tool, tool_name: str, payment_signature: str, resource_url: str,
) -> Union[dict, JSONResponse]:
    """PAYMENT-SIGNATURE header → settle on Base (Mode A: CDP, Mode B: JSON-RPC).

    Returns the auth dict on success, or a JSONResponse (402) on rejection.
    """
    if not settings.BASE_GATEWAY_ADDRESS:
        raise HTTPException(status_code=503, detail="Base payment not configured on this gateway")

    base_req = base_pay.build_payment_requirements(
        amount_usdc=tool.price_usdc,
        pay_to=settings.BASE_GATEWAY_ADDRESS,
        resource_url=resource_url,
        network=settings.BASE_NETWORK,
    )
    logger.info(f"[PAYMENT] tool={tool_name} network=base verifying PAYMENT-SIGNATURE header")
    bz = _TOOL_BAZAAR.get(tool.name, {})
    result = await base_pay.settle_base_payment(
        payment_signature, base_req, rpc_url=settings.BASE_RPC_URL,
        bazaar_resource=(
            {"url": resource_url, "mimeType": "application/json", **bz["resource"]}
            if bz.get("resource") else None
        ),
        bazaar_extension=bz.get("extension"),
    )
    if not result["success"]:
        status = "REPLAY_ATTACK" if result["reason"] == "replay_attack" else "FAILED"
        logger.info(f"[PAYMENT] tool={tool_name} network=base status={status} reason={result['reason']}")
        return JSONResponse(
            status_code=402,
            content={"error": "Base payment settlement failed", "reason": result["reason"]},
        )
    logger.info(f"[PAYMENT] tool={tool_name} network=base agent={result['payer'][:8]}... status=OK tx={result['tx_hash'][:16]}")
    return {
        "authorized": True,
        "tx_hash":    result["tx_hash"],
        "payer":      result["payer"],
        "network":    result["network"],
    }


async def _execute_and_log(
    tool, resolved: str, tool_name: str, body: ToolCallRequest,
    request: Request, auth: dict, agent_address: Optional[str],
    payment_id: str, is_base: bool,
) -> Union[dict, JSONResponse]:
    """Payment verified → run the tool, write the payment lifecycle, respond.

    For Stellar the payment_id is the UUID from the X-Payment header (matches
    the pre-402 row). For Base it's the tx_hash; the pre-402 UUID row is
    swept to 'abandoned' (x402-v2 doesn't echo the UUID back).
    """
    receipt_network = auth.get("network") or f"stellar-{settings.STELLAR_NETWORK}"
    client_ip       = request.client.host if request.client else None
    user_agent_str  = request.headers.get("user-agent")
    tx_hash         = auth.get("tx_hash", "")

    try:
        gateway_fee = str(
            Decimal(tool.price_usdc) * Decimal(str(settings.GATEWAY_FEE_PERCENT))
        )
    except Exception:
        gateway_fee = None

    # Resolved name so legacy aliases credit the canonical tool.
    registry.increment_call_count(resolved)

    # Base success strands the UUID-keyed pending row; INSERT a tx_hash-keyed
    # 'verified' row so the terminal PATCH has somewhere to land.
    if is_base and sb_enabled():
        asyncio.create_task(insert_pending_payment_log(
            payment_id=payment_id,
            tool_name=resolved,
            network=receipt_network,
            amount_usdc=tool.price_usdc,
            state="verified",
            agent_address=agent_address,
            tx_hash=tx_hash,
            developer_address=tool.developer_address or None,
            gateway_fee_usdc=gateway_fee,
            client_ip=client_ip,
            user_agent=user_agent_str,
        ))

    try:
        tool_result = await _run_tool(tool, resolved, tool_name, body.parameters)
    except Exception as e:
        return await _refund_and_502(tool_name, payment_id, e)

    append_transaction({
        "tool": tool_name,
        "amount_usdc": tool.price_usdc,
        "agent": agent_address,
        "tx_hash": tx_hash,
        "success": True,
    })
    agent_log = (agent_address or "unknown")[:8]
    logger.info(f"[CALL] tool={tool_name} agent={agent_log}... status=completed tx={tx_hash}")

    # Terminal 'payment_done' PATCH — awaited so analytics are consistent at
    # response time. The single Supabase write on the happy path.
    await update_payment_log_state(
        payment_id,
        "payment_done",
        network=receipt_network,
        agent_address=agent_address,
        tx_hash=tx_hash,
        developer_address=tool.developer_address or None,
        gateway_fee_usdc=gateway_fee,
        client_ip=client_ip,
        user_agent=user_agent_str,
    )

    return {
        "tool": tool_name,
        "result": tool_result,
        "payment": {
            "amount_usdc": tool.price_usdc,
            "tx_hash": auth.get("tx_hash"),
            "network": receipt_network,
        },
    }


@router.post("/tools/{tool_name}/call")
@limiter.limit("100/minute")                                        # per-IP
@limiter.limit(settings.WALLET_RATE_LIMIT, key_func=wallet_or_ip)  # per-wallet
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
      1. Neither header → _issue_402 (advertise both options)
      2. X-Payment → _settle_stellar, then _execute_and_log
      3. PAYMENT-SIGNATURE → _settle_base_path, then _execute_and_log
    """
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = registry.get_tool(resolved)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    if not tool.active:
        raise HTTPException(status_code=503, detail=f"Tool '{tool_name}' is currently unavailable")

    agent_address = x_agent_address or body.agent_address
    resource_url  = f"{GATEWAY_URL}/tools/{tool_name}/call"

    # x402-v2 clients (incl. SDK <= 0.2.3) send the SAME base64 v2 payload in
    # both X-PAYMENT and PAYMENT-SIGNATURE. If X-Payment isn't a parseable
    # Stellar proof and a PAYMENT-SIGNATURE is present, route to Base instead
    # of rejecting with 'Invalid X-Payment header format'.
    if x_payment and payment_signature and not parse_payment_header(x_payment):
        x_payment = None

    if not x_payment and not payment_signature:
        return await _issue_402(
            tool, resolved, tool_name, body, request, agent_address, resource_url,
        )

    if x_payment:
        auth = await _settle_stellar(tool_name, x_payment, agent_address)
        if isinstance(auth, JSONResponse):
            return auth
        parsed = parse_payment_header(x_payment) or {}
        payment_id = parsed.get("id") or auth.get("tx_hash", "")
        is_base = False
    else:
        auth = await _settle_base_path(tool, tool_name, payment_signature, resource_url)
        if isinstance(auth, JSONResponse):
            return auth
        agent_address = agent_address or auth["payer"]  # EVM payer for logging
        payment_id = auth.get("tx_hash", "")
        is_base = True

    return await _execute_and_log(
        tool, resolved, tool_name, body, request,
        auth, agent_address, payment_id, is_base,
    )


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
