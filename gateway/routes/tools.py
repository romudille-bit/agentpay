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

from decimal import Decimal

from gateway import base as base_pay
from gateway._limiter import limiter
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

    # x402-v2 clients (incl. SDK <= 0.2.3) send the SAME base64 v2 payload in
    # both X-PAYMENT (the x402 standard header) and PAYMENT-SIGNATURE. The
    # legacy Stellar parser can't read it, so without this guard the request
    # died with 'Invalid X-Payment header format' before the valid Base
    # signature was ever considered. If X-Payment isn't a parseable Stellar
    # proof and a PAYMENT-SIGNATURE is present, route to the Base path.
    if x_payment and payment_signature and not parse_payment_header(x_payment):
        x_payment = None

    # ── Step 1: No payment → 402 with both Stellar + Base options ────────────
    # Free tools (price_usdc == 0) issue a 402 too — they flow through the same
    # lifecycle as $0 payments so every call gets a payment_logs row and a
    # receipt (full visibility). The SDK skips on-chain settlement for $0 and
    # verify_and_fulfill authorizes $0 challenges without requiring a tx.
    if not x_payment and not payment_signature:
        agent_short = (agent_address or "unknown")[:8]
        logger.info(f"[CALL] tool={tool_name} agent={agent_short}... status=402_challenge")

        challenge = issue_payment_challenge(
            tool_name=tool_name,
            price_usdc=tool.price_usdc,
            developer_address=tool.developer_address,
            request_data={"parameters": body.parameters},
        )

        # ── PR #14: pre-402 payment_logs INSERT ──────────────────────────────
        # Insert a state='pending' row BEFORE returning the 402. Awaited and
        # fail-closed: if Supabase is enabled but the INSERT fails, return
        # 503 — we don't issue challenges we can't track. Closes the
        # analytics gap from §5.1 of the design doc (abandoned challenges
        # were previously invisible).
        #
        # Network is set to the Stellar default (the canonical UUID-keyed
        # row); a Base payment will produce a second row keyed on tx_hash
        # with network='base-...' since x402-v2 doesn't carry the UUID
        # through PAYMENT-SIGNATURE. The Stellar pending row gets swept to
        # 'abandoned' by _abandoned_sweep_loop in that case. Acceptable
        # for now; correlating Base back to the UUID is a future PR.
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
            # Build outputSchema for Bazaar auto-indexing. Bazaar reads this
            # from the PAYMENT-REQUIRED header on the first Base mainnet payment
            # through the CDP facilitator. Without it the listing has price but
            # no shape and ranks poorly.
            output_schema = None
            if tool.parameters or tool.response_example is not None:
                output_schema = {
                    "input":  tool.parameters or {},
                    "output": tool.response_example,
                }
            payment_required_header = base_pay.build_payment_required_header(
                requirements=base_req,
                resource_url=resource_url,
                tool_description=tool.description,
                output_schema=output_schema,
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

            # PR #14: PATCH the pending row to 'rejected' with error_reason.
            # AWAITED, not fire-and-forget. Per the Q3 decision in the
            # PR #14 plan, terminal states (payment_done, rejected,
            # refund_pending) are awaited so the analytics guarantee
            # holds at response time. The original v1 of this code
            # used asyncio.create_task — which loses the race on the
            # rejected branch specifically because there's no
            # downstream await before the return (unlike the verified
            # PATCH, which gets a yield during tool execution). Caught
            # by test_replay_attempt_marks_rejected failing on CI 3.10
            # where the TestClient event loop closed before the
            # scheduled task ran.
            #
            # The payment_id we PATCH on comes from the X-Payment header
            # (parsed by verify_and_fulfill); if the header was malformed
            # there's no UUID to PATCH and the pending row eventually
            # becomes 'abandoned' via the sweep.
            parsed = parse_payment_header(x_payment) or {}
            rejected_pid = parsed.get("id")
            if rejected_pid:
                await update_payment_log_state(
                    rejected_pid, "rejected", error_reason=auth["reason"],
                )

            return JSONResponse(
                status_code=402,
                content={"error": "Payment verification failed", "reason": auth["reason"]},
            )
        logger.info(f"[PAYMENT] tool={tool_name} network=stellar agent={agent_short}... status=OK tx={auth.get('tx_hash','')[:16]}")

        # PR #14: fire-and-forget PATCH to 'verified' (intermediate state,
        # per Q3 decision in pr-14-plan.md). Captures the agent_address +
        # tx_hash that arrived with the X-Payment header.
        #
        # PR #14a fix: expected_state='pending' guards against the race
        # where this fire-and-forget PATCH arrives AFTER the awaited
        # terminal payment_done PATCH at the end of this handler. Without
        # the guard, ~half of calls in production stuck at 'verified'
        # because the racing verified write landed after payment_done.
        verified_pid = (parse_payment_header(x_payment) or {}).get("id")
        if verified_pid:
            asyncio.create_task(update_payment_log_state(
                verified_pid, "verified",
                expected_state="pending",
                agent_address=agent_address,
                tx_hash=auth.get("tx_hash"),
            ))

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
    # Compute payment_id + receipt_network once; both the tool-failure and
    # tool-success branches need them. For Stellar the payment_id is the
    # UUID echoed back in the X-Payment header (matches the pre-402 row).
    # For Base, the original UUID isn't carried through PAYMENT-SIGNATURE,
    # so we key on tx_hash and the pre-402 UUID row stays at 'pending'
    # until the periodic sweep flips it to 'abandoned'. Acceptable
    # noise; correlating Base back to the UUID is a future protocol change.
    if x_payment:
        parsed = parse_payment_header(x_payment) or {}
        payment_id = parsed.get("id") or auth.get("tx_hash", "")
        is_base    = False
    else:
        payment_id = auth.get("tx_hash", "")
        is_base    = True

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

    # Use the resolved name for registry book-keeping so legacy aliases
    # (e.g. dex_liquidity) credit the canonical tool.
    registry.increment_call_count(resolved)

    # For Base success, the original UUID-keyed pending row is stranded
    # (x402-v2 doesn't carry the payment_id back). INSERT a second row
    # keyed on tx_hash with state='verified' so the eventual terminal
    # PATCH (payment_done / refund_pending) has somewhere to land. The
    # pre-402 UUID row eventually becomes 'abandoned' via the sweep.
    # Fire-and-forget for the same latency reasons as the Stellar
    # 'verified' PATCH above.
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
        # ── PR #14 + #12: tool failure post-verify → refund_pending ─────────
        # The payment was accepted on-chain but the tool execution failed.
        # PATCH the row to 'refund_pending' (awaited — terminal as far as
        # the request handler is concerned; the background refund worker
        # in main.py:lifespan picks it up from here when REFUND_ENABLED).
        # error_reason captures what went wrong for analytics + post-mortem
        # AND for the worker to log if the row eventually fails refund too.
        logger.error(f"Tool execution error: {e}")
        await update_payment_log_state(
            payment_id,
            "refund_pending",
            error_reason=f"tool_exec_failed: {str(e)[:200]}",
        )
        # PR #12: structured 502 body. Existing clients that just check
        # response.status_code keep seeing an error. New SDK versions
        # (v0.1.4+) read the body to surface payment_status to user code:
        #   payment_status="refund_pending"  → refund queued, will land in ~60s
        #   payment_status="refund_disabled" → refund flag off; manual
        #                                     reconciliation needed
        return JSONResponse(
            status_code=502,
            content={
                "error":               "Tool execution failed",
                "tool":                tool_name,
                "payment_id":          payment_id,
                "payment_status":      "refund_pending" if settings.REFUND_ENABLED else "refund_disabled",
                "refund_eta_seconds":  60 if settings.REFUND_ENABLED else None,
                "error_reason":        f"tool_exec_failed: {str(e)[:200]}",
            },
        )

    # ── Step 4: Log transaction ───────────────────────────────────────────────
    append_transaction({
        "tool": tool_name,
        "amount_usdc": tool.price_usdc,
        "agent": agent_address,
        "tx_hash": tx_hash,
        "success": True,
    })
    agent_log = (agent_address or "unknown")[:8]
    logger.info(f"[CALL] tool={tool_name} agent={agent_log}... status=completed tx={tx_hash}")

    # ── PR #14: terminal 'payment_done' PATCH ─────────────────────────────────
    # Awaited (per Q3 decision: terminal states are awaited so analytics
    # are guaranteed consistent at response time). Populates the
    # state-machine columns introduced by #13a — network / client_ip /
    # user_agent / gateway_fee_usdc / developer_address — which the
    # pre-402 INSERT couldn't know yet at challenge-issue time.
    #
    # log_payment (the legacy single-INSERT helper) is gone; this PATCH
    # is the single Supabase write on the happy path.
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
