"""
routes/session.py — Session creation endpoint.

  POST /v1/session/create — Register a budget-capped agent session.
                            Priced at $0.01 USDC via x402 (Base or Stellar).
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

SESSION_PRICE_USDC  = "0.01"
SESSION_TOOL_NAME   = "session_create"
SESSION_RESOURCE_URL = f"{GATEWAY_URL}/v1/session/create"

_SESSION_DESCRIPTION = (
    "A stateful, multi-chain spending session for AI agents. One session "
    "enforces a hard USDC budget cap across every tool call, with a "
    "verifiable receipt and running ledger for each payment — not a "
    "one-shot budget check, but persistent spend governance with a full "
    "audit trail. USDC settles on Base or Stellar. Costs $0.01 USDC once; "
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
            "amount_usdc": "0.01",
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
    # Top-level description mirrors indexed resources (their bazaar block is
    # {description, info, schema}); also carried on resource.description.
    "description": "A stateful, multi-chain spending session for AI agents: a hard USDC budget cap across every tool call, with a verifiable receipt and running ledger per payment. Not a one-shot budget check — persistent spend governance with a full audit trail. USDC on Base or Stellar.",
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
                    "amount_usdc": "0.01",
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


# ── 402 challenge builder (shared by POST no-payment branch + GET probe) ──────

def _session_402_payload(challenge) -> tuple[dict, dict]:
    """
    Build the (content, headers) for a session_create 402 challenge.

    Pure — no DB writes. Used both by the POST no-payment branch (which also
    persists a pending row) and by the GET discovery probe (which does not).
    Always advertises the Base option + PAYMENT-REQUIRED header when Base is
    configured, so x402 indexers (Bazaar) can validate the resource on a plain
    GET the same way they would on a POST.
    """
    resource_url = SESSION_RESOURCE_URL
    base_option = None
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
            # Expose the Bazaar extension + serviceName/tags on the LIVE 402 so
            # discovery crawlers can validate the resource (settle-only metadata
            # left it stuck in 'processing'). Mirrors indexed competitors.
            bazaar_resource=_SESSION_BAZAAR_RESOURCE,
            bazaar_extension=_SESSION_BAZAAR_EXTENSION,
        )

    headers = build_402_headers(challenge)
    if payment_required_header:
        headers["PAYMENT-REQUIRED"] = payment_required_header

    # Stellar option (always available as a fallback / secondary chain).
    stellar_option = {
        "payment_id":  challenge.payment_id,
        "amount_usdc": challenge.amount_usdc,
        "pay_to":      challenge.gateway_address,
        "network":     settings.STELLAR_NETWORK,
        "asset":       "USDC",
        "header":      f"X-Payment: tx_hash=<hash>,from=<addr>,id={challenge.payment_id}",
    }
    stellar_instructions = (
        f"[Stellar] Send {challenge.amount_usdc} USDC to {challenge.gateway_address} "
        f"on Stellar {settings.STELLAR_NETWORK} with memo: {challenge.payment_id}. "
        f"Retry with X-Payment: tx_hash=<hash>,from=<addr>,id={challenge.payment_id}."
    )

    content = {
        "error":       "Payment required",
        "x402Version": 2,
    }

    if base_option:
        # ── Lead with Base (canonical paid chain; CDP/Bazaar-native). ──────────
        # Generic x402 clients read the TOP-LEVEL network/pay_to/instructions as
        # the default offer, so those must advertise Base to match the
        # PAYMENT-REQUIRED header and the "Base is the canonical paid chain"
        # strategy. Stellar stays available as the secondary payment_option.
        # `payment_id` is still surfaced at top level for backward-compat clients
        # that key off it (Stellar memo).
        content.update({
            "network":      base_option["network"],
            "pay_to":       base_option["pay_to"],
            "asset":        base_option["asset"],
            "amount_usdc":  base_option["amount_usdc"],
            "instructions": base_option["instructions"],
            "payment_id":   challenge.payment_id,
            # ── Structured multi-chain options (Base first) ───────────────────
            "payment_options": {
                "base":    base_option,
                "stellar": stellar_option,
            },
        })
    else:
        # ── No Base configured → fall back to leading with Stellar. ────────────
        content.update({
            "payment_id":   challenge.payment_id,
            "amount_usdc":  challenge.amount_usdc,
            "pay_to":       challenge.gateway_address,
            "asset":        "USDC",
            "network":      settings.STELLAR_NETWORK,
            "instructions": stellar_instructions,
            "payment_options": {
                "stellar": stellar_option,
            },
        })
    return content, headers


# ── Discovery probe: GET returns the 402 challenge (no DB row) ────────────────
# x402 indexers (Bazaar) validate a resource by GETting its URL and expecting a
# 402 with payment requirements. The paid flow is POST-only, so without this a
# crawler GET hit 405 and the listing could never be validated → stuck in
# 'processing'. This handler advertises the same challenge for discovery without
# persisting a pending payment row.
@router.get("/v1/session/create")
@limiter.limit("60/minute")
async def session_create_probe(request: Request):
    challenge = issue_payment_challenge(
        tool_name=SESSION_TOOL_NAME,
        price_usdc=SESSION_PRICE_USDC,
        developer_address=settings.GATEWAY_PUBLIC_KEY,
        request_data={"max_spend": "0.10"},
    )
    content, headers = _session_402_payload(challenge)
    return JSONResponse(status_code=402, content=content, headers=headers)


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
    Open a budget-capped agent session for $0.01 USDC.

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

    # Standards-pure x402 clients (Coinbase for Agents, x402 SDKs) send the
    # v2 payload in X-PAYMENT alone; route it to the Base path instead of
    # the legacy Stellar parser.
    from gateway.routes.tools import normalize_payment_headers
    x_payment, payment_signature = normalize_payment_headers(x_payment, payment_signature)

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

        # Persist pending row in Supabase (fail-closed same as tools route).
        # Record the chain we actually LEAD the 402 with (Base when configured,
        # else Stellar) rather than hardcoding Stellar — otherwise every
        # abandoned-at-402 row is mislabelled stellar-mainnet and analytics
        # can't tell Base-intent from Stellar-intent abandoners. The terminal
        # payment_done PATCH still overwrites this with the chain that actually
        # settled, so completed rows stay accurate.
        offered_network = (
            f"base-{settings.BASE_NETWORK}" if settings.BASE_GATEWAY_ADDRESS
            else f"stellar-{settings.STELLAR_NETWORK}"
        )
        if sb_enabled():
            client_ip  = request.client.host if request.client else None
            user_agent = request.headers.get("user-agent")
            row_id = await insert_pending_payment_log(
                payment_id=challenge.payment_id,
                tool_name=SESSION_TOOL_NAME,
                network=offered_network,
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

        # Build the 402 challenge body + headers (Base option + PAYMENT-REQUIRED
        # included for Bazaar CDP Facilitator indexing).
        content, headers = _session_402_payload(challenge)
        return JSONResponse(status_code=402, content=content, headers=headers)

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
        # The settle result's payer is VERIFIED (Mode A: CDP-attested EIP-3009
        # signer; Mode B: bound to the Transfer log's from-topic). The body/
        # header-declared agent_address is NOT — real buyers were logged as
        # docs-example addresses (0x742d35Cc…, 0x0000…0) they'd copy-pasted
        # into the request body. Verified payer wins; declared is fallback.
        agent_address = result["payer"] or agent_address

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
        # In-band upsell — buyers are wallets, not emails; the response
        # payload is the only channel that reliably reaches them.
        "related": {
            "hint": ("Price any multi-tool plan for free before spending: "
                     "POST /v1/plan/estimate"),
            "paid_tools": [
                {"tool": "verified_route", "price_usdc": "0.01",
                 "why": ("usage-vetted pick of the best x402 provider for any "
                         "need — sybil tails collapsed, ready-to-pay challenge "
                         "included")},
                {"tool": "pre_trade_check", "price_usdc": "0.01",
                 "why": ("one-call ok/caution/avoid trade verdict: live "
                         "slippage at your size + funding + OI + security")},
            ],
        },
    }
