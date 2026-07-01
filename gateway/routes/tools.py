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
  _settle_free_v2    — PAYMENT-SIGNATURE on a $0 tool → accept standard x402
                       payload as the free proof, no on-chain settlement
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
    record_tx_hash,
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


def normalize_payment_headers(
    x_payment: Optional[str], payment_signature: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Route the x402 v2 payload to the Base path regardless of which header
    carried it.

    X-PAYMENT is the x402 STANDARD header — pure-spec clients (Coinbase for
    Agents, x402 SDKs) send the base64 v2 payload there and nothing else.
    AgentPay's legacy Stellar proof shares the same header name. Rules:
      - X-Payment parses as a legacy Stellar proof → leave untouched.
      - X-Payment is a v2 payload (alone or duplicated into
        PAYMENT-SIGNATURE) → treat it as PAYMENT-SIGNATURE.
      - X-Payment is garbage → leave it for the legacy path's clear error.
    """
    if not x_payment or parse_payment_header(x_payment):
        return x_payment, payment_signature
    if payment_signature:
        return None, payment_signature          # duplicate of the v2 sig
    decoded, _err = base_pay._decode_payment_signature(x_payment)
    if isinstance(decoded, dict) and ("payload" in decoded or "tx_hash" in decoded):
        return None, x_payment                  # standards-pure v2 client
    return x_payment, payment_signature


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
            # Schema follows the Bazaar convention every indexed resource
            # uses: `input` describes the HTTP REQUEST ENVELOPE (type/method/
            # bodyType/body), not the bare tool params. Validation appears to
            # enforce this shape — the params-only variant stayed stuck in
            # 'processing' while session_create (envelope shape) indexed.
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "input": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "type":     {"const": "http", "type": "string"},
                            "method":   {"enum": ["POST"], "type": "string"},
                            "bodyType": {"enum": ["json", "form-data", "text"], "type": "string"},
                            "body": {
                                "type": "object",
                                "properties": {
                                    "parameters": {
                                        "type": "object",
                                        "properties": {
                                            "symbol": {
                                                "type": "string",
                                                "description": "Asset to check, e.g. 'ETH', 'BTC', 'SOL'",
                                            },
                                            "size_usd": {
                                                "type": "number",
                                                "description": "Intended position size in USD (drives the slippage check; default 10000)",
                                            },
                                            "side": {
                                                "type": "string",
                                                "enum": ["long", "short"],
                                                "description": "Trade direction (funding carry is side-aware; default long)",
                                            },
                                            "token_address": {
                                                "type": "string",
                                                "description": "Optional ERC-20 address — adds a GoPlus security scan",
                                            },
                                        },
                                        "required": ["symbol"],
                                    },
                                },
                            },
                        },
                        "required": ["type", "bodyType", "body", "method"],
                    },
                    "output": {
                        "type": "object",
                        "properties": {
                            "example": {"type": "object"},
                            "type":    {"type": "string"},
                        },
                        "required": ["type"],
                    },
                },
                "required": ["input"],
            },
        },
    },
    "verified_route": {
        "resource": {
            "serviceName": "AgentPay",
            "description": (
                "Buyer-side trust oracle for the x402 marketplace: 'I need X, "
                "budget $Y — which tool is real?' Sweeps the WHOLE catalog across "
                "many queries, collapses sybil/factory clusters (one wallet "
                "stamping many fake-distinct listings), ranks the genuinely-used "
                "survivors by unique-payer usage, and returns one vetted pick with "
                "a ready-to-pay challenge. The credit-bureau check an agent can't "
                "do in a single query."
            ),
            # ≤5 tags, ≤32 chars each — own the routing/trust category, not data.
            "tags": ["x402-routing", "trust-oracle", "sybil-detection",
                     "tool-discovery", "agent-commerce"],
        },
        "extension": {
            "description": (
                "Vet before you pay a stranger: sweep the x402 catalog, collapse "
                "sybil/factory listings, rank real providers by usage, and return "
                "ONE recommendation under budget with a ready-to-pay x402 challenge "
                "+ a catalog dossier (scanned / real_providers / sybil_collapsed / "
                "biggest_factory)."
            ),
            "info": {
                "input": {
                    "type":     "http",
                    "method":   "POST",
                    "bodyType": "json",
                    "body": {
                        "parameters": {
                            "need":       "dex pair liquidity",
                            "budget_usd": 1,
                            "chain":      "",
                        },
                    },
                },
                "output": {
                    "type": "json",
                    "example": {
                        "need": "dex pair liquidity",
                        "recommendation": {
                            "name": "Otto AI", "url": "https://otto.example/dex",
                            "price_usd": "0.001", "network": "eip155:8453",
                            "payers30d": 200, "calls30d": 3246, "quality": 3851,
                            "ready_to_pay": {"url": "https://otto.example/dex",
                                             "network": "eip155:8453",
                                             "accepts": {"scheme": "exact",
                                                         "network": "eip155:8453"}},
                        },
                        "catalog": {"scanned": 117, "real_providers": 41,
                                    "sybil_collapsed": 73,
                                    "biggest_factory": {"pay_to": "0x2bb72231eed3",
                                                        "listings": 72}},
                        "vetting": "swept 17 queries → 117 listings → collapsed 73 "
                                   "sybil listings → 41 real providers",
                    },
                },
            },
            # Same HTTP-envelope schema convention every indexed resource uses —
            # `input` describes the REQUEST envelope, not the bare params.
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "input": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "type":     {"const": "http", "type": "string"},
                            "method":   {"enum": ["POST"], "type": "string"},
                            "bodyType": {"enum": ["json", "form-data", "text"], "type": "string"},
                            "body": {
                                "type": "object",
                                "properties": {
                                    "parameters": {
                                        "type": "object",
                                        "properties": {
                                            "need": {
                                                "type": "string",
                                                "description": "What the agent needs, e.g. 'dex pair liquidity', 'crypto prices'",
                                            },
                                            "budget_usd": {
                                                "type": "number",
                                                "description": "Max USDC to pay the downstream tool per call (default 1)",
                                            },
                                            "chain": {
                                                "type": "string",
                                                "description": "Optional chain filter: 'base', 'arbitrum'. Empty = all chains",
                                            },
                                        },
                                        "required": ["need"],
                                    },
                                },
                            },
                        },
                        "required": ["type", "bodyType", "body", "method"],
                    },
                    "output": {
                        "type": "object",
                        "properties": {
                            "example": {"type": "object"},
                            "type":    {"type": "string"},
                        },
                        "required": ["type"],
                    },
                },
                "required": ["input"],
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
            (
                "This tool is FREE ($0). Sign a standard $0 EIP-3009 "
                "transferWithAuthorization, encode as base64 JSON PaymentPayload, "
                "and retry with header X-PAYMENT or PAYMENT-SIGNATURE — the "
                "gateway accepts it without any on-chain settlement (no funds "
                "move, no gas). No wallet balance required."
            )
            if Decimal(str(tool.price_usdc or "0")) == 0
            else (
                "Sign an EIP-3009 transferWithAuthorization for the amount above, "
                "encode as base64 JSON PaymentPayload, and retry with header "
                "PAYMENT-SIGNATURE: <base64_payload>"
            )
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


# In-memory fast guard for _settle_free_v2 nonce consumption (mirrors
# _used_base_tx_hashes in gateway/base.py — single-process guard when
# Supabase is disabled/unreachable).
_used_free_v2_nonces: set[str] = set()


async def _settle_free_v2(
    tool_name: str, payment_signature: str,
) -> Union[dict, JSONResponse]:
    """PAYMENT-SIGNATURE on a FREE ($0) tool → fulfil WITHOUT on-chain settlement.

    Wall E fix (2026-07-01). Standards-pure x402 clients (x402-fetch, Coinbase
    for Agents, plain-`node` agents) can't speak AgentPay's `free:<id>`
    X-Payment dialect. They read the 402's PAYMENT-REQUIRED accepts, sign a $0
    EIP-3009 authorization, and send the standard base64 v2 payload. Routing
    that into _settle_base_path attempts a real CDP/JSON-RPC settlement of a
    $0 transfer, which always fails — ~6k free calls/month bounced on this
    (see FUNNEL_FINDINGS_2026-07.md).

    There is no money to verify on a $0 challenge, so a well-formed v2 payload
    IS the free proof: consume its EIP-3009 nonce for replay/dedup (atomic
    in-memory check-and-add + awaited insert-only record_tx_hash, same pattern
    as the paid paths) and hand back an auth dict. The full payment_logs
    lifecycle is preserved exactly like the paid Base path: the pre-402 UUID
    row is swept to 'abandoned', _execute_and_log inserts a tx-keyed row and
    PATCHes it to payment_done. Nothing moves on-chain — the unsettled
    authorization simply expires (EIP-3009 validity ≤300s).

    Payer identity is self-reported (signature not recovered) — the same trust
    level as the SDK free flow's `from=` field. NEVER route priced tools here.
    """
    payload, err = base_pay._decode_payment_signature(payment_signature)
    if err or not isinstance(payload, dict):
        return JSONResponse(
            status_code=402,
            content={"error": "Free-tool payment payload invalid",
                     "reason": err or "not_a_json_object"},
        )

    authorization = {}
    inner = payload.get("payload")
    if isinstance(inner, dict) and isinstance(inner.get("authorization"), dict):
        authorization = inner["authorization"]

    payer = str(authorization.get("from") or payload.get("payer") or "")
    # Mode A payloads carry a unique EIP-3009 nonce; Mode B ones a tx_hash.
    # Either is a usable dedup key. Cap length defensively (DB column hygiene).
    nonce = str(authorization.get("nonce") or payload.get("tx_hash") or "")[:80]
    if not nonce:
        return JSONResponse(
            status_code=402,
            content={
                "error":  "Free-tool payment payload invalid",
                "reason": "missing_authorization_nonce",
                "hint":   ("This tool is free ($0). Sign a standard $0 EIP-3009 "
                           "authorization from the PAYMENT-REQUIRED accepts and retry "
                           "with X-PAYMENT or PAYMENT-SIGNATURE; it is accepted "
                           "without on-chain settlement."),
            },
        )

    free_key = f"free:{nonce}"
    # Atomic consume — in-memory check-and-add (no await in between), then the
    # awaited insert-only record. A 409 (record_tx_hash → False) means another
    # worker already consumed this nonce → replay.
    if free_key in _used_free_v2_nonces:
        return JSONResponse(
            status_code=402,
            content={"error": "Payment verification failed",
                     "reason": "Payment already used (replay attack)"},
        )
    _used_free_v2_nonces.add(free_key)
    if sb_enabled():
        recorded = await record_tx_hash(free_key, "free")
        if recorded is False:
            return JSONResponse(
                status_code=402,
                content={"error": "Payment verification failed",
                         "reason": "Payment already used (replay attack)"},
            )

    network = str(payload.get("network") or "free")
    logger.info(
        f"[PAYMENT] tool={tool_name} network=free-v2 agent={(payer or 'unknown')[:8]}... "
        f"status=OK (standard $0 payload, no settlement) key={free_key[:20]}"
    )
    return {
        "authorized": True,
        "tx_hash":    free_key,
        "payer":      payer or "v2-free-unknown",
        "network":    network,
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


@router.get("/tools/{tool_name}/call")
@limiter.limit("60/minute")
async def call_tool_get(tool_name: str, request: Request):
    """x402 discovery crawlers probe resources with GET — answer with the
    same 402 challenge POST issues, so the validation crawl can read the
    PAYMENT-REQUIRED header (incl. extensions.bazaar). Without this the
    crawl gets a 405 and the listing never leaves 'processing' — the exact
    failure session_create had before GET /v1/session/create existed.
    """
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = registry.get_tool(resolved)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    if not tool.active:
        raise HTTPException(status_code=503, detail=f"Tool '{tool_name}' is currently unavailable")
    return await _issue_402(
        tool, resolved, tool_name, ToolCallRequest(), request,
        None, f"{GATEWAY_URL}/tools/{tool_name}/call",
    )


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
      3. PAYMENT-SIGNATURE + $0 tool → _settle_free_v2 (no on-chain settle)
      4. PAYMENT-SIGNATURE → _settle_base_path, then _execute_and_log
    """
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = registry.get_tool(resolved)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    if not tool.active:
        raise HTTPException(status_code=503, detail=f"Tool '{tool_name}' is currently unavailable")

    agent_address = x_agent_address or body.agent_address
    resource_url  = f"{GATEWAY_URL}/tools/{tool_name}/call"

    x_payment, payment_signature = normalize_payment_headers(x_payment, payment_signature)

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
        try:
            _is_free_tool = Decimal(str(tool.price_usdc or "0")) == 0
        except Exception:
            _is_free_tool = False
        if _is_free_tool:
            # Wall E fix: standard v2 payload on a $0 tool — accept as the
            # free proof, never attempt a real settlement of $0. Nothing is
            # verified on a $0 call, so the declared address may keep priority.
            auth = await _settle_free_v2(tool_name, payment_signature)
            if isinstance(auth, JSONResponse):
                return auth
            agent_address = agent_address or auth["payer"]
        else:
            auth = await _settle_base_path(tool, tool_name, payment_signature, resource_url)
            if isinstance(auth, JSONResponse):
                return auth
            # The settle result's payer is VERIFIED (Mode A: CDP-attested
            # EIP-3009 signer; Mode B: bound to the Transfer log's from-topic).
            # The declared agent_address is NOT — real buyers were logged as
            # docs-example addresses (0x742d35Cc…, 0x0000…0) copy-pasted into
            # the request. Verified payer wins; declared is fallback.
            agent_address = auth["payer"] or agent_address
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
