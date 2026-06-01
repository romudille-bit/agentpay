"""
routes/session.py — Session creation endpoint.

  POST /v1/session/create — Register a budget-capped agent session.
                            Priced at $0.001 USDC via x402 (Base or Stellar).
                            Returns session_id + budget config.
                            Indexed on Base Bazaar via CDP Facilitator.

This is AgentPay's "Session as a product" endpoint — agents pay once to open a
session, then call any tool within their max_spend budget cap. The endpoint is
priced so it gets settled through the CDP x402 Facilitator, which triggers
Bazaar auto-indexing. Agents that discover AgentPay on Bazaar will see
`/v1/session/create` as the entry point.
"""

import asyncio
import uuid
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gateway import base as base_pay
from gateway._limiter import limiter
from gateway.config import GATEWAY_URL, settings
from gateway.services.supabase import (
    insert_pending_payment_log,
    sb_enabled,
    update_payment_log_state,
)
from gateway.x402 import (
    build_402_headers,
    issue_payment_challenge,
    parse_payment_header,
    verify_and_fulfill,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Constants ─────────────────────────────────────────────────────────────────

SESSION_PRICE_USDC  = "0.001"
SESSION_TOOL_NAME   = "session_create"
SESSION_RESOURCE_URL = f"{GATEWAY_URL}/v1/session/create"

_SESSION_DESCRIPTION = (
    "A stateful, multi-chain spending session for AI agents. One session "
    "enforces a hard USDC budget cap across every tool call, with a "
    "verifiable receipt and running ledger for each payment — not a "
    "one-shot budget check, but persistent spend governance with a full "
    "audit trail. USDC settles on Base or Stellar. Costs $0.001 USDC once; "
    "returns a session_id and budget config. Enforce the cap client-side "
    "with the AgentPay SDK (from agentpay import Session)."
)

_SESSION_OUTPUT_SCHEMA = {
    "input": {
        "type": "object",
        "properties": {
            "agent_address": {"type": "string", "description": "Paying agent's wallet address"},
            "max_spend":     {"type": "string", "description": "Budget cap in USDC, e.g. '0.10'"},
            "label":         {"type": "string", "description": "Optional session label"},
        },
    },
    "output": {
        "session_id":    "uuid",
        "max_spend":     "0.10",
        "agent_address": "G...",
        "gateway_url":   "https://agentpay.tools",
        "created_at":    "2026-05-27T00:00:00Z",
        "tools_endpoint": "https://agentpay.tools/tools",
        "receipt": {
            "tx_hash":     "0x...",
            "network":     "base",
            "amount_usdc": "0.001",
        },
    },
}


# ── Bazaar indexing metadata (injected server-side into every CDP settle) ─────
# Bazaar reads paymentPayload.resource + .extensions.bazaar at settle time to
# auto-index this resource in discovery. We set these on the gateway so indexing
# fires on EVERY session_create payment, not just clients that include them.
# serviceName <= 32 chars; tags <= 5 entries, each <= 32 chars.
_SESSION_BAZAAR_RESOURCE = {
    "url":         SESSION_RESOURCE_URL,
    "description": "A stateful, multi-chain spending session for AI agents. One session enforces a hard USDC budget cap across every tool call, with a verifiable receipt and running ledger for each payment — not a one-shot budget check, but persistent spend governance with a full audit trail. USDC on Base or Stellar.",
    "mimeType":    "application/json",
    "serviceName": "AgentPay",
    # ≤5 tags, ≤32 chars each — own the governance category, not the data-API commodity.
    "tags":        ["spend-control", "agent-budget", "payment-receipts", "spend-authorization", "agent-commerce"],
}

_SESSION_BAZAAR_EXTENSION = {
    "info": {
        "input": {
            "type":     "http",
            "method":   "POST",
            "bodyType": "json",
            "body": {
                "agent_address": "0x0000000000000000000000000000000000000000",
                "max_spend":     "0.10",
                "label":         "session",
            },
        },
        "output": {
            "type": "json",
            "example": {
                "session_id":     "f47ac10b-58cc-4372-a567-0e02b2c3d479",
                "max_spend":      "0.10",
                "gateway_url":    GATEWAY_URL,
                "tools_endpoint": f"{GATEWAY_URL}/tools",
                "created_at":     "2026-05-29T00:00:00Z",
                "receipt": {
                    "tx_hash":     "0xee85d8dd374b5d1cb40bfa441086af557d356acc2bb4d5819f56331fce42adee",
                    "network":     "eip155:8453",
                    "amount_usdc": "0.001",
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
                "required": ["type", "method", "body"],
                "properties": {
                    "type":     {"type": "string"},
                    "method":   {"type": "string"},
                    "bodyType": {"type": "string"},
                    "body": {
                        "type": "object",
                        "required": ["agent_address"],
                        "properties": {
                            "agent_address": {"type": "string", "description": "Paying agent's EVM wallet address"},
                            "max_spend":     {"type": "string", "description": "Hard budget cap in USDC, e.g. '0.10'"},
                            "label":         {"type": "string", "description": "Optional human-readable session label"},
                        },
                    },
                },
            },
        },
    },
}


# ── Request model ─────────────────────────────────────────────────────────────

class SessionCreateRequest(BaseModel):
    agent_address: Optional[str] = None
    max_spend: str = "0.10"      # Agent's budget cap for this session
    label: Optional[str] = None  # Optional human label for this session


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/v1/session/create")
@limiter.limit("30/minute")
async def create_session(
    body: SessionCreateRequest,
    request: Request,
    x_payment: Optional[str] = Header(None),
    x_agent_address: Optional[str] = Header(None),
    payment_signature: Optional[str] = Header(None),   # x402 v2 Base/EVM
):
    """
    Open a budget-capped agent session for $0.001 USDC.

    Supports two payment paths:
      Stellar — X-Payment: tx_hash=<hash>,from=<addr>,id=<payment_id>
      Base    — PAYMENT-SIGNATURE: <base64(PaymentPayload JSON)>

    On verified payment returns:
      session_id       — unique UUID for this session
      max_spend        — budget cap from request body
      agent_address    — paying agent's address
      gateway_url      — root of agentpay.tools
      tools_endpoint   — where to call tools
      created_at       — ISO 8601 timestamp
      receipt          — tx_hash + network + amount
    """
    agent_address = x_agent_address or body.agent_address
    resource_url  = SESSION_RESOURCE_URL

    # ── Step 1: No payment → 402 ──────────────────────────────────────────────
    if not x_payment and not payment_signature:
        agent_short = (agent_address or "unknown")[:8]
        logger.info(f"[SESSION] agent={agent_short}... status=402_challenge")

        challenge = issue_payment_challenge(
            tool_name=SESSION_TOOL_NAME,
            price_usdc=SESSION_PRICE_USDC,
            developer_address=settings.GATEWAY_PUBLIC_KEY,
            request_data={"max_spend": body.max_spend},
        )

        # Persist pending row in Supabase (fail-closed same as tools route)
        if sb_enabled():
            client_ip  = request.client.host if request.client else None
            user_agent = request.headers.get("user-agent")
            row_id = await insert_pending_payment_log(
                payment_id=challenge.payment_id,
                tool_name=SESSION_TOOL_NAME,
                network=f"stellar-{settings.STELLAR_NETWORK}",
                amount_usdc=SESSION_PRICE_USDC,
                developer_address=settings.GATEWAY_PUBLIC_KEY,
                client_ip=client_ip,
                user_agent=user_agent,
            )
            if row_id is None:
                raise HTTPException(
                    status_code=503,
                    detail="Supabase write failure — challenge issuance refused. Retry shortly.",
                )

        # Build Base payment option (required for Bazaar CDP Facilitator indexing)
        base_option            = None
        payment_required_header = None
        if settings.BASE_GATEWAY_ADDRESS:
            base_req = base_pay.build_payment_requirements(
                amount_usdc=SESSION_PRICE_USDC,
                pay_to=settings.BASE_GATEWAY_ADDRESS,
                resource_url=resource_url,
                network=settings.BASE_NETWORK,
            )
            base_option = {
                "scheme":            base_req["scheme"],
                "network":           base_req["network"],
                "amount_atomic":     base_req["amount"],
                "amount_usdc":       SESSION_PRICE_USDC,
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
                tool_description=_SESSION_DESCRIPTION,
                output_schema=_SESSION_OUTPUT_SCHEMA,
            )

        headers = build_402_headers(challenge)
        if payment_required_header:
            headers["PAYMENT-REQUIRED"] = payment_required_header

        return JSONResponse(
            status_code=402,
            content={
                "error":       "Payment required",
                "x402Version": 2,
                # ── Stellar option (backward-compat) ──────────────────────────
                "payment_id":  challenge.payment_id,
                "amount_usdc": challenge.amount_usdc,
                "pay_to":      challenge.gateway_address,
                "asset":       "USDC",
                "network":     settings.STELLAR_NETWORK,
                "instructions": (
                    f"[Stellar] Send {challenge.amount_usdc} USDC to {challenge.gateway_address} "
                    f"on Stellar {settings.STELLAR_NETWORK} with memo: {challenge.payment_id}. "
                    f"Retry with X-Payment: tx_hash=<hash>,from=<addr>,id={challenge.payment_id}."
                ),
                # ── Structured multi-chain options ────────────────────────────
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
            },
            headers=headers,
        )

    # ── Step 2a: Stellar payment ───────────────────────────────────────────────
    if x_payment:
        if not agent_address:
            raise HTTPException(
                status_code=400,
                detail="agent_address required (body field or X-Agent-Address header)",
            )

        agent_short = (agent_address or "unknown")[:8]
        logger.info(f"[SESSION] agent={agent_short}... verifying Stellar X-Payment")
        auth = await verify_and_fulfill(payment_header=x_payment, agent_address=agent_address)

        if not auth["authorized"]:
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
        logger.info(f"[SESSION] agent={agent_short}... stellar OK tx={auth.get('tx_hash','')[:16]}")

        # Fire-and-forget 'verified' intermediate state
        verified_pid = (parse_payment_header(x_payment) or {}).get("id")
        if verified_pid:
            asyncio.create_task(update_payment_log_state(
                verified_pid, "verified",
                expected_state="pending",
                agent_address=agent_address,
                tx_hash=auth.get("tx_hash"),
            ))

    # ── Step 2b: Base/EVM payment ─────────────────────────────────────────────
    elif payment_signature:
        if not settings.BASE_GATEWAY_ADDRESS:
            raise HTTPException(status_code=503, detail="Base payment not configured on this gateway")

        base_req = base_pay.build_payment_requirements(
            amount_usdc=SESSION_PRICE_USDC,
            pay_to=settings.BASE_GATEWAY_ADDRESS,
            resource_url=resource_url,
            network=settings.BASE_NETWORK,
        )
        logger.info("[SESSION] verifying Base PAYMENT-SIGNATURE")
        result = await base_pay.settle_base_payment(
            payment_signature, base_req, rpc_url=settings.BASE_RPC_URL,
            bazaar_resource=_SESSION_BAZAAR_RESOURCE,
            bazaar_extension=_SESSION_BAZAAR_EXTENSION,
        )

        if not result["success"]:
            status = "REPLAY_ATTACK" if result["reason"] == "replay_attack" else "FAILED"
            logger.info(f"[SESSION] base status={status} reason={result['reason']}")
            return JSONResponse(
                status_code=402,
                content={"error": "Base payment settlement failed", "reason": result["reason"]},
            )
        logger.info(f"[SESSION] base OK payer={result['payer'][:8]}... tx={result['tx_hash'][:16]}")
        auth = {
            "authorized": True,
            "tx_hash":    result["tx_hash"],
            "payer":      result["payer"],
            "network":    result["network"],
        }
        agent_address = agent_address or result["payer"]

    # ── Step 3: Payment verified → create session ─────────────────────────────
    if x_payment:
        parsed      = parse_payment_header(x_payment) or {}
        payment_id  = parsed.get("id") or auth.get("tx_hash", "")
        is_base     = False
    else:
        payment_id  = auth.get("tx_hash", "")
        is_base     = True

    receipt_network = auth.get("network") or f"stellar-{settings.STELLAR_NETWORK}"
    tx_hash         = auth.get("tx_hash", "")

    # Base path: insert a tx_hash-keyed row so the terminal PATCH below has
    # somewhere to land (same pattern as the tools route).
    if is_base and sb_enabled():
        asyncio.create_task(insert_pending_payment_log(
            payment_id=payment_id,
            tool_name=SESSION_TOOL_NAME,
            network=receipt_network,
            amount_usdc=SESSION_PRICE_USDC,
            state="verified",
            agent_address=agent_address,
            tx_hash=tx_hash,
            developer_address=settings.GATEWAY_PUBLIC_KEY,
        ))

    session_id = str(uuid.uuid4())
    created_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        gateway_fee = str(Decimal(SESSION_PRICE_USDC) * Decimal(str(settings.GATEWAY_FEE_PERCENT)))
    except Exception:
        gateway_fee = None

    # Terminal PATCH — awaited so analytics are consistent at response time
    await update_payment_log_state(
        payment_id,
        "payment_done",
        network=receipt_network,
        agent_address=agent_address,
        tx_hash=tx_hash,
        developer_address=settings.GATEWAY_PUBLIC_KEY,
        gateway_fee_usdc=gateway_fee,
    )

    logger.info(f"[SESSION] created session_id={session_id[:8]}... agent={str(agent_address or '')[:8]}... tx={tx_hash[:16]}")

    return {
        "session_id":     session_id,
        "max_spend":      body.max_spend,
        "agent_address":  agent_address,
        "label":          body.label,
        "gateway_url":    GATEWAY_URL,
        "tools_endpoint": f"{GATEWAY_URL}/tools",
        "created_at":     created_at,
        "receipt": {
            "tx_hash":     tx_hash,
            "network":     receipt_network,
            "amount_usdc": SESSION_PRICE_USDC,
        },
        "sdk_hint": "Use `from agentpay import Session` to enforce the max_spend cap client-side.",
    }
