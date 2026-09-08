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
import json
import time
import hmac
import ipaddress
import logging
import re
import socket
from typing import Optional, Union
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError, model_validator

import registry

from dataclasses import replace
from decimal import Decimal

from gateway import base as base_pay
from gateway import stacks as stacks_pay
from gateway._limiter import limiter, wallet_or_ip
from gateway.config import GATEWAY_URL, settings
from gateway.services.supabase import (
    correlate_pending_challenge,
    get_payment_log,
    insert_pending_payment_log,
    record_payment_id,
    persist_tool_registration,
    record_tx_hash,
    sb_enabled,
    update_payment_log_state,
)
from gateway.services import probe_rollup
from gateway.services.tools_runtime import real_tool_response
from gateway.services.transaction_log import append_transaction
from gateway.x402 import (
    _lookup_challenge,
    build_402_headers,
    issue_payment_challenge,
    parse_payment_header,
    verify_and_fulfill,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Models ────────────────────────────────────────────────────────────────────

class ToolCallRequest(BaseModel):
    """Body of POST /tools/{name}/call.

    Canonical shape nests tool arguments under "parameters", but some
    clients send them at the top level ({"symbol": "SOL"} instead of
    {"parameters": {"symbol": "SOL"}}). Pydantic used to silently discard
    unknown keys, so those calls ran on tool defaults with no error. The
    validator below folds unrecognized top-level keys into `parameters`
    whenever `parameters` itself is empty, so both shapes work; nested
    callers are byte-for-byte unaffected.
    """

    parameters: dict = {}
    agent_address: Optional[str] = None  # Agent's Stellar wallet address

    @model_validator(mode="before")
    @classmethod
    def _fold_bare_body(cls, data):
        if isinstance(data, dict) and not data.get("parameters"):
            extras = {
                k: v for k, v in data.items()
                if k not in ("parameters", "agent_address")
            }
            if extras:
                data = dict(data)
                data["parameters"] = extras
        return data


async def parse_body_after_payment_gate(
    request: Request, model_cls, *, strict: bool,
):
    """Parse the request body into `model_cls` after the payment-gate decision.

    FastAPI's declarative body binding validates before the handler runs, so
    an unpaid bare POST, empty body, or non-JSON content type would get a 422
    instead of the 402 challenge. The body is therefore read here, after the
    gate, to keep the invariant: an unpaid request to a paid resource always
    returns 402; body validation happens only on the paid path, before
    settlement.

    Returns (model_instance, None) or (None, JSONResponse-422).

    strict=False (unpaid → 402 path): any body — absent, empty, non-JSON,
    non-dict, or model-invalid — folds to model defaults. The challenge is
    issued regardless of body shape.

    strict=True (payment header present): a malformed body returns a
    FastAPI-shaped 422 before any on-chain settlement, so a payer never burns
    a real payment on a call the gateway cannot execute. An absent/empty body
    is accepted as model defaults (every field on both models has one — the
    caller is asking for the tool's default behaviour, which is a legitimate
    paid call).
    """
    raw = await request.body()
    if not raw:
        return model_cls(), None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        if strict:
            return None, JSONResponse(
                status_code=422,
                content={"detail": [{
                    "type": "json_invalid",
                    "loc":  ["body"],
                    "msg":  "Request body is not valid JSON",
                }]},
            )
        return model_cls(), None
    try:
        return model_cls.model_validate(data), None
    except ValidationError as e:
        if strict:
            return None, JSONResponse(
                status_code=422,
                content={"detail": jsonable_encoder(e.errors(include_url=False))},
            )
        return model_cls(), None


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


# ── In-band upsell (paid responses only) ─────────────────────────────────────
# Paying buyers are wallets, not emails — the response payload is the only
# channel that reliably reaches them. Every paid response carries one compact,
# deterministic pointer to the complementary paid tools + the free plan
# estimator. Kept off free responses (200x the volume; don't nag).
_RELATED_HINT = (
    "Price any multi-tool plan for free before spending: "
    "POST /v1/plan/estimate"
)
_PAID_RELATED: dict[str, list[dict]] = {
    "verified_route": [
        {"tool": "pre_trade_check", "price_usdc": "0.01",
         "why": ("one-call ok/caution/avoid trade verdict: live slippage at "
                 "your size + side-aware funding + OI crowding + security")},
        {"tool": "session_create", "price_usdc": "0.01",
         "why": "hard multi-call spend cap with a verifiable receipt ledger"},
    ],
    "pre_trade_check": [
        {"tool": "verified_route", "price_usdc": "0.01",
         "why": ("usage-vetted pick of the best x402 provider for any need — "
                 "sybil tails collapsed, ready-to-pay challenge included")},
        {"tool": "session_create", "price_usdc": "0.01",
         "why": "hard multi-call spend cap with a verifiable receipt ledger"},
    ],
}


def normalize_payment_headers(
    x_payment: Optional[str], payment_signature: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Route the x402 v2 payload to the Base path regardless of which header
    carried it.

    X-PAYMENT is the x402 standard header — pure-spec clients (Coinbase for
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
# from the live 402, and indexing fires on a Mode A settle that carries the
# extension. Tools listed here get both injected (mirrors routes/session.py).
_TOOL_BAZAAR: dict[str, dict] = {
    "pre_trade_check": {
        "resource": {
            # Bazaar ranks head terms on name + description text, not on tags,
            # so the search terms this tool should surface for ("trading",
            # "risk") appear in the copy below as well as in the tag list.
            "serviceName": "AgentPay Pre-Trade Risk Check",
            "description": (
                "Pre-trade risk check for AI agents trading crypto: live orderbook "
                "slippage at YOUR size, side-aware funding carry, open-interest "
                "crowding, and optional contract security — composed into a single "
                "ok/caution/avoid verdict with per-factor reasons and raw data "
                "embedded. Answers 'is this trade sane?' before trading, not after. "
                "Replaces four API integrations plus the judgment layer."
            ),
            # ≤5 tags, ≤32 chars each. Exact-tag matching surfaces a listing
            # for rare compounds where competition is thin; for common head
            # terms, text relevance over name+description dominates and a tag
            # contributes little on its own.
            "tags": ["trading", "trade", "risk", "slippage", "pre-trade-check"],
        },
        "extension": {
            "description": (
                "Pre-trade risk check for AI agents trading crypto: 'I want to "
                "long $X of SYMBOL — is now sane?' Live slippage at your size + "
                "side-aware funding carry + OI crowding + optional security scan "
                "→ one ok/caution/avoid verdict with per-factor breakdown and raw "
                "components embedded."
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
            # uses: `input` describes the HTTP request envelope (type/method/
            # bodyType/body), not the bare tool params. Validation appears to
            # enforce this shape — a params-only variant never leaves
            # 'processing'.
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
            # Head terms are won by keyword-in-name, so the service name says
            # what the tool is ("Trust Oracle") rather than relying on tags.
            "serviceName": "AgentPay x402 Trust Oracle",
            "description": (
                "Buyer-side trust oracle for the x402 marketplace: 'I need X, "
                "budget $Y — which tool is real?' Sweeps the WHOLE catalog across "
                "many queries, collapses sybil/factory clusters (one wallet "
                "stamping many fake-distinct listings), ranks the genuinely-used "
                "survivors by unique-payer usage, and returns one vetted pick with "
                "a ready-to-pay challenge. The credit-bureau check an agent can't "
                "do in a single query."
            ),
            # ≤5 tags, ≤32 chars each — own the routing/trust category, not
            # data. Plain words match real queries (Bazaar is exact-tag match);
            # compounds keep the category label.
            "tags": ["routing", "discovery", "trust",
                     "trust-oracle", "sybil-detection"],
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
            # `input` describes the request envelope, not the bare params.
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
                                                "description": "Optional chain filter: 'base', 'arbitrum', 'solana'. Empty = all chains. Solana picks are discovery-only (probe coverage: Base only — on-chain delivery unverified)",
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

# Documented error responses are part of CDP's agent-ready curation bar.
# One shared catalogue (gateway.base.build_error_responses) is injected into
# every listed resource's extensions.bazaar info block, so the live 402 and
# the settle payload both carry it.
for _bz_cfg in _TOOL_BAZAAR.values():
    _bz_cfg["extension"].setdefault("info", {}).setdefault(
        "errors", base_pay.build_error_responses())
del _bz_cfg


# ── Routes ────────────────────────────────────────────────────────────────────

def _wants_html(request: Request) -> bool:
    """Browser/crawler content negotiation — same rule as the root route.

    JSON stays the default (agents, SDK, tests all send Accept: */* or
    application/json). Explicit text/html (browsers) gets the page — and so
    do known search crawlers, because bingbot & co. send Accept: */* (Bing
    flagged the JSON responses as 'HTML document missing a title tag').
    An explicit application/json Accept always wins, so an agent identifying
    as anything still gets JSON by asking for it.
    """
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return False
    if "text/html" in accept:
        return True
    from gateway.tool_pages import is_search_crawler
    return is_search_crawler(request.headers.get("user-agent", ""))


def _demo_price_overrides() -> dict:
    """Testnet-only paid-tool prices for the Stacks demo. Parses
    settings.TESTNET_PAID_TOOLS ('token_price:0.01,foo:0.02') into {name: price}.
    Empty on mainnet (env unset) so the free-funnel registry prices are untouched."""
    raw = (settings.TESTNET_PAID_TOOLS or "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        name, sep, price = pair.partition(":")
        name, price = name.strip(), price.strip()
        if sep and name and price:
            out[name] = price
    return out


def _apply_demo_pricing(tool):
    """Return the tool with its testnet demo price applied, else unchanged.
    None-safe (an unresolved tool passes straight through)."""
    if tool is None:
        return tool
    price = _demo_price_overrides().get(tool.name)
    if price is None or price == tool.price_usdc:
        return tool
    return replace(tool, price_usdc=price)


@router.get("/tools")
async def list_tools(request: Request, category: Optional[str] = None):
    """List all available tools with pricing (HTML for browsers, JSON default)."""
    tools = [_apply_demo_pricing(t) for t in registry.list_tools(category=category)]
    if _wants_html(request):
        from gateway.tool_pages import render_tools_index
        return Response(content=render_tools_index(tools, GATEWAY_URL),
                        media_type="text/html",
                        headers={"Cache-Control": "public, max-age=300",
                                 "Vary": "Accept, User-Agent"})
    return JSONResponse(content={
        "tools": [registry.tool_to_dict(t) for t in tools],
        "count": len(tools),
    }, headers={"Vary": "Accept, User-Agent"})


@router.api_route("/tools/{tool_name}", methods=["GET", "HEAD"])
async def get_tool(tool_name: str, request: Request):
    """Get details for a specific tool. Supports legacy aliases.

    Browsers (Accept: text/html) get a server-rendered, indexable page;
    agents keep the JSON contract.
    """
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = _apply_demo_pricing(registry.get_tool(resolved))
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    if _wants_html(request):
        from gateway.tool_pages import render_tool_page
        return Response(content=render_tool_page(tool, GATEWAY_URL),
                        media_type="text/html",
                        headers={"Cache-Control": "public, max-age=300",
                                 "Vary": "Accept, User-Agent"})
    return JSONResponse(content=registry.tool_to_dict(tool),
                        headers={"Vary": "Accept, User-Agent"})


@router.head("/tools/{tool_name}/call")
async def head_tool(tool_name: str, request: Request):
    """
    HEAD pre-flight for x402 discovery — answers 402, mirroring the GET probe.

    Probers scoring "does the resource return 402?" treat any other status as
    a failure, so HEAD returns the same 402 challenge as GET
    /tools/{name}/call (status + PAYMENT-REQUIRED header incl.
    extensions.bazaar, no pending row) with an empty body per HEAD semantics.
    The X-Price-USDC/X-Pay-To pre-flight headers are kept for cost-check
    callers.
    """
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = _apply_demo_pricing(registry.get_tool(resolved))
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    if not tool.active:
        raise HTTPException(status_code=503, detail=f"Tool '{tool_name}' is currently unavailable")

    challenge_resp = await _issue_402(
        tool, resolved, tool_name, ToolCallRequest(), request,
        None, f"{GATEWAY_URL}/tools/{tool_name}/call",
        log_pending=False,
    )
    # Empty-body 402: keep the challenge headers, drop the entity headers the
    # JSONResponse computed for its (discarded) body.
    headers = {
        k: v for k, v in challenge_resp.headers.items()
        if k.lower() not in ("content-length", "content-type")
    }
    headers.update({
        "X-Price-USDC":        tool.price_usdc,
        "X-Asset":             "USDC",
        "X-Network":           f"stellar-{settings.STELLAR_NETWORK}",
        "X-Pay-To":            settings.GATEWAY_PUBLIC_KEY,
        "X-Payment-Required":  "true",
        "X-Tool-Name":         tool_name,
        "X-Tool-Category":     tool.category,
    })
    if settings.BASE_GATEWAY_ADDRESS:
        headers["X-Base-Network"] = settings.BASE_NETWORK
        headers["X-Base-Pay-To"]  = settings.BASE_GATEWAY_ADDRESS
    return Response(status_code=402, headers=headers)


# ── Payment-flow stages (orchestrated by call_tool) ──────────────────────────

def _base_402_option(tool, resource_url: str):
    """Build the Base payment option + PAYMENT-REQUIRED header for a 402.

    Returns (base_option, payment_required_header, accepts_entry), all None
    when BASE_GATEWAY_ADDRESS isn't configured. `accepts_entry` is the
    standard-form x402 entry (payTo/maxAmountRequired) for the 402 JSON body,
    so generic x402 payers can discover the Base path without decoding the
    PAYMENT-REQUIRED header.
    """
    if not settings.BASE_GATEWAY_ADDRESS:
        return None, None, None

    base_req = base_pay.build_payment_requirements(
        amount_usdc=tool.price_usdc,
        pay_to=settings.BASE_GATEWAY_ADDRESS,
        resource_url=resource_url,
        network=settings.BASE_NETWORK,
        description=tool.description,
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
    bz = _bazaar_for(tool.name)
    payment_required_header = base_pay.build_payment_required_header(
        requirements=base_req,
        resource_url=resource_url,
        tool_description=tool.description,
        output_schema=output_schema,
        bazaar_resource=bz.get("resource"),
        bazaar_extension=bz.get("extension"),
    )
    accepts_entry = base_pay.build_accepts_entry(
        requirements=base_req,
        resource_url=resource_url,
        description=tool.description,
    )
    return base_option, payment_required_header, accepts_entry


def _bazaar_for(tool_name: str) -> dict:
    """Bazaar resource/extension for a tool. session_create is also payable at
    /v1/session/create; both paths declare the same canonical resource,
    otherwise a settle here indexes a second, unnamed entry."""
    if tool_name == "session_create":
        from gateway.routes.session import (_SESSION_BAZAAR_EXTENSION,
                                            _SESSION_BAZAAR_RESOURCE)
        return {"resource": _SESSION_BAZAAR_RESOURCE,
                "extension": _SESSION_BAZAAR_EXTENSION}
    return _TOOL_BAZAAR.get(tool_name, {})


async def _refund_and_500(tool_name: str, payment_id: str, exc: Exception) -> JSONResponse:
    """Payment accepted on-chain but tool execution failed → refund_pending.

    The PATCH is awaited (terminal state); the background refund worker
    picks the row up when REFUND_ENABLED. The body carries payment_status
    so SDK callers can branch (RefundPending exception). 500, not 502:
    Cloudflare replaces an origin 502/504 with its own HTML page, and the
    body would never reach the SDK.
    """
    logger.error(f"Tool execution error: {exc}")
    await update_payment_log_state(
        payment_id,
        "refund_pending",
        error_reason=f"tool_exec_failed: {str(exc)[:200]}",
    )
    return JSONResponse(
        status_code=500,
        content={
            "error":               "Tool execution failed",
            "tool":                tool_name,
            "payment_id":          payment_id,
            "payment_status":      "refund_pending" if settings.REFUND_ENABLED else "refund_disabled",
            "refund_eta_seconds":  60 if settings.REFUND_ENABLED else None,
            "error_reason":        f"tool_exec_failed: {str(exc)[:200]}",
        },
    )


# ── Endpoint safety (SSRF guard) ─────────────────────────────────────────────

def _endpoint_is_safe(url: str) -> tuple[bool, str]:
    """True when `url` is an https endpoint whose host resolves only to
    public addresses. Blocks SSRF to loopback/private/link-local/metadata
    (169.254.169.254 is link-local) targets. Blocking DNS work — call via
    asyncio.to_thread from async code.

    Used at registration time (reject early with a clear error) and at call
    time in _run_tool (re-resolved per call, so a DNS-rebinding endpoint
    that turned private after registration is still blocked)."""
    try:
        p = urlparse(url)
    except Exception:
        return False, "unparseable URL"
    if p.scheme != "https":
        return False, "endpoint must be https"
    host = p.hostname
    if not host:
        return False, "endpoint has no host"
    try:
        infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return False, f"endpoint host does not resolve: {e}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False, "endpoint resolved to an unparseable address"
        # `not is_global` rather than enumerating flags: is_private misses
        # 100.64.0.0/10 (CGNAT), which hosting providers' internal fabrics
        # ride on. is_global is False for every special-purpose range
        # (private, loopback, link-local, CGNAT, reserved, multicast,
        # unspecified, ...).
        if not ip.is_global:
            return False, f"endpoint resolves to a non-public address ({ip})"
    return True, "ok"


async def _run_tool(tool, resolved: str, tool_name: str, parameters: dict):
    """Execute the tool — proxy endpoint when configured, real APIs otherwise."""
    if not tool.endpoint:
        return await real_tool_response(resolved, parameters)
    # Call-time SSRF guard. Registration validates too, but the check re-runs
    # on every call so DNS-rebinding endpoints (and tools registered before
    # the guard) are covered. An unsafe endpoint degrades to the real-API
    # fallback, same as an unreachable one.
    safe, why = await asyncio.to_thread(_endpoint_is_safe, tool.endpoint)
    if not safe:
        logger.warning(
            f"Tool endpoint blocked ({why}) for {tool_name} — calling real APIs"
        )
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
    log_pending: bool = True,
) -> JSONResponse:
    """No payment header → issue a 402 challenge with Stellar + Base options.

    Free tools (price_usdc == 0) issue a 402 too — they flow through the same
    lifecycle as $0 payments so every call gets a payment_logs row and a
    receipt. The SDK skips on-chain settlement for $0 and verify_and_fulfill
    authorizes $0 challenges without requiring a tx.

    Issuing a 402 writes nothing to payment_logs: the row is created at
    settle time ('verified' in _execute_and_log, or 'rejected' via
    _record_rejected_attempt), so the table only holds real payment
    attempts and a Supabase blip cannot 503 a challenge. 402 volume —
    overwhelmingly monitors and scanners that never pay — is counted in
    probe_rollup. The pending_challenges mirror is written only for a paid
    POST from an identified payer (persist=); it is what lets a paying agent
    straddle a worker restart. log_pending=False marks a GET discovery
    probe, which never persists anything.
    """
    agent_short = (agent_address or "unknown")[:8]
    logger.info(f"[CALL] tool={tool_name} agent={agent_short}... status=402_challenge")

    try:
        is_free = Decimal(str(tool.price_usdc or "0")) == 0
    except Exception:
        is_free = False

    # Stacks quote first so it rides on the challenge (durable across a
    # restart). Bounded: the option is optional, a slow quote must not slow
    # the 402.
    stacks_offer = None
    if stacks_pay.stacks_offerable(tool.price_usdc):
        try:
            stacks_offer = await asyncio.wait_for(
                stacks_pay.stacks_offer(tool.price_usdc), timeout=4.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[CALL] tool={tool_name} stacks quote timed out — "
                           "402 issued without the stacks option")
    stacks_quote = stacks_offer[:2] if stacks_offer else None

    # The durable mirror exists so a paying agent can straddle a restart.
    # Payers identify themselves on the first POST (the SDK and the npm
    # client send agent_address); monitors that never pay send bare
    # parameters. Anonymous callers keep the in-memory challenge.
    challenge = issue_payment_challenge(
        tool_name=tool_name,
        price_usdc=tool.price_usdc,
        developer_address=tool.developer_address,
        request_data={"parameters": body.parameters},
        persist=(log_pending and not is_free and bool(agent_address)),
        stacks_quote=stacks_quote,
    )

    # Aggregate telemetry for every 402 issued — the durable record of
    # probe/demand volume, since bot 402s write no per-event rows.
    probe_rollup.record_402(
        tool_name=resolved,
        user_agent=request.headers.get("user-agent"),
        kind=("probe_get" if not log_pending
              else ("free_402" if is_free else "paid_402")),
    )

    # No pre-402 payment_logs insert (see the docstring); the fail-closed
    # guarantee lives where the money is, in the replay-store consume.

    base_option, payment_required_header, accepts_entry = _base_402_option(tool, resource_url)

    stacks_option = (
        stacks_pay.stacks_402_option(stacks_quote, tool.price_usdc,
                                     fee_microstx=stacks_offer[2])
        if stacks_offer else None
    )

    headers = build_402_headers(challenge)
    if payment_required_header:
        headers["PAYMENT-REQUIRED"] = payment_required_header

    # Mirror the resource-info block into the 402 JSON body: trust validators
    # parse the body, not the base64 header. Same builder as the header so
    # the two cannot drift; independent of Base config so Stellar-only 402s
    # carry it too.
    bz = _bazaar_for(tool.name)
    resource_block = base_pay.build_resource_block(
        resource_url, tool.description, bz.get("resource"),
    )

    body_content = {
        "error":       "Payment required",
        "x402Version": 2,
        "resource":    resource_block,
        # Standard x402 accepts[] in the body (not just the PAYMENT-REQUIRED
        # header) so generic payers find the Base path.
        "accepts":     [accepts_entry] if accepts_entry else [],
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
            # Scheme named honestly: a classic Stellar payment with a text
            # memo verified via Horizon, not the standard @x402/stellar
            # Soroban scheme. Standard @x402/stellar clients cannot pay this
            # option; the AgentPay SDK and manual payments can.
            "stellar": {
                "scheme":      "agentpay-classic-memo",
                "payment_id":  challenge.payment_id,
                "amount_usdc": challenge.amount_usdc,
                "pay_to":      challenge.gateway_address,
                "network":     settings.STELLAR_NETWORK,
                "asset":       "USDC",
                "header":      f"X-Payment: tx_hash=<hash>,from=<addr>,id={challenge.payment_id}",
                "note": (
                    "Classic Stellar payment + text memo (payment_id), "
                    "verified via Horizon — not the standard @x402/stellar "
                    "Soroban scheme. Pay with the AgentPay SDK (pip install "
                    "agentpay-x402) or manually per instructions."
                ),
            },
            **({"base": base_option} if base_option else {}),
            # Stacks/sBTC: only when configured and quotable
            # (docs/stacks-adapter.md).
            **({"stacks": stacks_option} if stacks_option else {}),
        },
    }
    # Mirror extensions.bazaar into the body too (header parity) — additive;
    # validators/indexers that read the body see the same envelope.
    if bz.get("extension"):
        body_content["extensions"] = {"bazaar": bz["extension"]}

    return JSONResponse(status_code=402, content=body_content, headers=headers)


# Rejection reasons that carry no analytics value: the payment_id doesn't
# correspond to any known challenge (scanner garbage / long-expired probe) or
# the header never parsed. Recording these per-event would only generate
# bot write churn.
_REJECTION_NOISE_MARKERS = (
    "not found or expired",
    "invalid x-payment header",
)


async def _record_rejected_attempt(
    payment_id: str,
    reason: str,
    *,
    tool_name: str,
    network: str,
    amount_usdc: str,
    agent_address: Optional[str] = None,
) -> None:
    """Durably record a rejected real payment attempt.

    There is no pre-402 pending row to PATCH, so a rejected attempt would
    otherwise vanish from payment_logs. Transition-safe two-step:
      1. PATCH pending/verified → 'rejected' — covers legacy rows, with the
         expected_state guard so a header-supplied pid can never clobber a
         terminal row.
      2. If the PATCH confirmed 0 matches, INSERT a complete 'rejected'
         row — unless the reason marks it as noise (unknown payment_id /
         unparseable header), which is scanner traffic, not an attempt.
    On an unknown PATCH outcome (None) nothing is inserted: a transient blip
    must not mint duplicate rows.
    """
    if not sb_enabled():
        return
    matched = await update_payment_log_state(
        payment_id, "rejected", error_reason=reason,
        expected_state=("pending", "verified"),
    )
    if matched != 0:
        return
    low = (reason or "").lower()
    if any(m in low for m in _REJECTION_NOISE_MARKERS):
        return
    await insert_pending_payment_log(
        payment_id=payment_id,
        tool_name=tool_name,
        network=network,
        amount_usdc=amount_usdc,
        state="rejected",
        agent_address=agent_address,
        error_reason=reason,
    )


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

        # Terminal states are awaited so analytics are consistent at response
        # time (a create_task here loses the race: there is no downstream
        # await before the return). _record_rejected_attempt PATCHes a legacy
        # row if one exists and otherwise INSERTs a complete 'rejected' row,
        # unless the reason marks scanner noise.
        rejected_pid = (parse_payment_header(x_payment) or {}).get("id")
        if rejected_pid:
            _t = registry.get_tool(tool_name)
            await _record_rejected_attempt(
                rejected_pid, auth["reason"],
                tool_name=tool_name,
                network=f"stellar-{settings.STELLAR_NETWORK}",
                amount_usdc=str(getattr(_t, "price_usdc", "0") or "0"),
                agent_address=agent_address,
            )

        return JSONResponse(
            status_code=402,
            content={"error": "Payment verification failed", "reason": auth["reason"]},
        )
    logger.info(f"[PAYMENT] tool={tool_name} network=stellar agent={agent_short}... status=OK tx={auth.get('tx_hash','')[:16]}")

    # No intermediate 'verified' PATCH here: there is no pending row to
    # advance. _execute_and_log inserts the state='verified' row for every
    # paid settle (both rails) and the terminal PATCH lands on it.
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
        description=tool.description,
    )
    logger.info(f"[PAYMENT] tool={tool_name} network=base verifying PAYMENT-SIGNATURE header")
    bz = _bazaar_for(tool.name)
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


def _stacks_reject(reason: str, status: int = 402) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": "Stacks payment settlement failed",
                 "payment_status": "rejected", "error_reason": reason},
    )


def _stacks_uncertain(reason: str, payment_id: str, txid: str) -> JSONResponse:
    # 503, not 502: Cloudflare swaps an origin 502/504 for its own HTML page
    # and the SDK would never see this body.
    return JSONResponse(status_code=503, content={
        "error": "Stacks settlement uncertain",
        "payment_status": "uncertain",
        "error_reason": reason,
        "payment_id": payment_id,
        "txid": txid,
        "redeem": "re-present the same payment-signature once the tx confirms",
    })


async def _redeem_uncertain_stacks(
    tool, tool_name: str, signed_tx: bytes,
) -> Optional[Union[dict, JSONResponse]]:
    """Redemption for a settle that ended uncertain: the same signed bytes
    are presented again after the tx confirmed. The `payment_logs` row keyed
    on the recomputed txid, in state 'uncertain', is the proof this gateway
    verified and broadcast exactly these bytes; delivery happens once, via
    the uncertain→verified compare-and-set. None when no such row exists.
    """
    if not sb_enabled():
        return None
    txid = stacks_pay.txid_of(signed_tx)
    row = await get_payment_log(txid)
    if not row or row.get("state") != "uncertain":
        return None
    if row.get("tool_name") not in (tool.name, tool_name):
        return _stacks_reject("redeem_tool_mismatch")
    confirm = await stacks_pay.poll_confirmation(txid, max_polls=2)
    if confirm["status"] == "rejected":
        await update_payment_log_state(txid, "rejected", expected_state="uncertain",
                                       error_reason=confirm["reason"])
        return _stacks_reject(confirm["reason"])
    if confirm["status"] != "success":
        return _stacks_uncertain("still_unconfirmed", row.get("payment_id") or "", txid)
    claimed = await update_payment_log_state(txid, "verified", expected_state="uncertain",
                                             clear_fields=["error_reason"])
    if not claimed:
        return _stacks_reject("already_redeemed")
    logger.info(f"[PAYMENT] tool={tool_name} network=stacks status=REDEEMED tx={txid[:16]}")
    return {
        "authorized": True,
        "tx_hash":    txid,
        "payer":      row.get("agent_address") or "",
        "network":    row.get("network") or f"stacks-{settings.STACKS_NETWORK}",
        "recovered":  True,
        "redeemed":   True,
    }


async def _settle_stacks_path(
    tool, tool_name: str, payment_signature: str, payload: dict,
    parameters: Optional[dict] = None,
) -> Union[dict, JSONResponse]:
    """Stacks payment-signature payload → verify, consume, broadcast, confirm.
    `payload` is the already-decoded payment-signature JSON (the dispatcher
    decoded it to route on network="stacks:…").

    Returns the auth dict on success, or a JSONResponse on failure whose
    body carries the payment_status the SDK keys on (docs/stacks-adapter.md
    §Wire contract): "rejected" → nothing broadcast or settleable, the SDK
    zeroes the leg (re-signing once on a nonce conflict); "uncertain" → the
    tx may be live, the spend stays recorded and the same header redeems it
    once confirmed.
    """
    if not stacks_pay.stacks_configured():
        raise HTTPException(status_code=503,
                            detail="Stacks payment not configured on this gateway")
    _reject = _stacks_reject

    try:
        signed_tx = bytes.fromhex((payload.get("payload") or {}).get("signedTransaction") or "")
    except (ValueError, TypeError, AttributeError):
        signed_tx = b""
    if not signed_tx:
        return _reject("missing_or_invalid_signed_transaction")

    # ── payment_id binding: the payload names the challenge; the memo inside
    # the signed tx must match it (verified below), and the challenge fixes
    # the expected amount. Missing/unknown id ⇒ nothing to verify against.
    payment_id = str(payload.get("payment_id") or "")
    if not payment_id:
        return _reject("missing_payment_id")
    challenge = await _lookup_challenge(payment_id)
    expired = bool(challenge and challenge.get("expires_at")
                   and challenge["expires_at"] < time.time())
    if challenge is None or expired:
        redeemed = await _redeem_uncertain_stacks(tool, tool_name, signed_tx)
        if redeemed is not None:
            return redeemed
        return _reject("challenge_expired" if expired else "unknown_or_expired_payment_id")
    if challenge.get("tool_name") and challenge["tool_name"] not in (tool.name, tool_name):
        return _reject("challenge_tool_mismatch")

    # Verify against the quote recorded on the challenge at issuance, not a
    # fresh re-quote: a BTC move between issue and settle must not fail the
    # amount check. Re-quote only when the challenge carries no quote
    # (issued without a Stacks option).
    if challenge.get("stacks_sats"):
        expected_sats = int(challenge["stacks_sats"])
        quote_rate = str(challenge.get("stacks_rate") or "")
    else:
        requote = await stacks_pay.stacks_quote(
            challenge.get("amount_usdc") or tool.price_usdc)
        if requote is None:
            raise HTTPException(status_code=503,
                                detail="Stacks pricing unavailable on this gateway")
        expected_sats, quote_rate = requote[0], str(requote[1])

    logger.info(f"[PAYMENT] tool={tool_name} network=stacks verifying "
                f"payment-signature (payment {payment_id[:8]}…)")
    auth = await stacks_pay.verify_stacks_payment(
        payment_signature,
        expected_amount_sats=expected_sats,
        expected_recipient=settings.STACKS_GATEWAY_ADDRESS,
        payment_id=payment_id,
    )
    if not auth["authorized"]:
        logger.info(f"[PAYMENT] tool={tool_name} network=stacks status=FAILED "
                    f"reason={auth['reason']}")
        # No pending row — record the rejected attempt (payment_id is bound
        # to a known challenge here, so this is a real attempt, never
        # scanner noise).
        await _record_rejected_attempt(
            payment_id, auth["reason"],
            tool_name=tool.name,
            network=f"stacks-{settings.STACKS_NETWORK}",
            amount_usdc=str(tool.price_usdc or "0"),
        )
        return _reject(auth["reason"])

    # ── consume the challenge before broadcast (fail closed): a second tx
    # against the same payment_id must never double-fulfil. The txid consume
    # inside settle_stacks_payment guards the tx itself.
    if sb_enabled():
        pid_recorded = await record_payment_id(payment_id)
        if pid_recorded is False:
            redeemed = await _redeem_uncertain_stacks(tool, tool_name, signed_tx)
            if redeemed is not None:
                return redeemed
            return _reject("payment_id_already_used_replay")
        if pid_recorded is None:
            # Nothing broadcast, nothing consumed: the SDK confirms the tx is
            # absent on Hiro, zeroes the leg and signs again.
            return _reject("replay_store_unavailable: nothing was broadcast "
                           "— request a fresh 402 and sign again")

    settle = await stacks_pay.settle_stacks_payment(
        signed_tx, auth["txid"], payment_id=payment_id,
        payment_payload=payload,
        requirements={
            "scheme": "exact",
            "network": (payload.get("network") or ""),
            "amount": str(auth["amount_sats"]),
            "asset": "sbtc",
            "payTo": settings.STACKS_GATEWAY_ADDRESS,
        },
    )
    if not settle["ok"]:
        status = "REPLAY_ATTACK" if settle["reason"] == "replay_attack" else "FAILED"
        logger.info(f"[PAYMENT] tool={tool_name} network=stacks status={status} "
                    f"state={settle['state']} reason={settle['reason']}")
        if settle["state"] == "rejected":
            await _record_rejected_attempt(
                payment_id, settle["reason"],
                tool_name=tool.name,
                network=f"stacks-{settings.STACKS_NETWORK}",
                amount_usdc=str(tool.price_usdc or "0"),
            )
            return _reject(settle["reason"])
        # uncertain → the tx may be live. Record it so the payer can redeem
        # (same header, once confirmed) and the ledger never loses a
        # broadcast; the SDK keeps the spend recorded meanwhile.
        if sb_enabled() and settle["reason"] != "replay_attack":
            await insert_pending_payment_log(
                payment_id=settle["txid"],
                tool_name=tool.name,
                network=f"stacks-{settings.STACKS_NETWORK}",
                amount_usdc=str(tool.price_usdc or "0"),
                state="uncertain",
                agent_address=auth["sender"],
                tx_hash=settle["txid"],
                developer_address=tool.developer_address or None,
                error_reason=f"{settle['reason']} challenge={payment_id}",
                parameters=parameters or None,
            )
        return _stacks_uncertain(settle["reason"], payment_id, settle["txid"])

    logger.info(f"[PAYMENT] tool={tool_name} network=stacks "
                f"agent={auth['sender'][:8]}... status=OK "
                f"state={settle['state']} tx={settle['txid'][:16]}")
    return {
        "authorized": True,
        "tx_hash":    settle["txid"],
        "payer":      auth["sender"],
        "network":    f"stacks-{settings.STACKS_NETWORK}",
        "recovered":  settle["state"] == "ok_recovered",
        "amount_sats":  auth["amount_sats"],
        "btc_usd_rate": quote_rate,
    }


# In-memory fast guard for _settle_free_v2 nonce consumption (mirrors
# _used_base_tx_hashes in gateway/base.py — single-process guard when
# Supabase is disabled/unreachable).
_used_free_v2_nonces: set[str] = set()


async def _settle_free_v2(
    tool_name: str, payment_signature: str,
) -> Union[dict, JSONResponse]:
    """PAYMENT-SIGNATURE on a free ($0) tool → fulfil without on-chain settlement.

    Standards-pure x402 clients (x402-fetch, Coinbase for Agents) do not
    speak AgentPay's `free:<id>` X-Payment dialect. They read the 402's
    PAYMENT-REQUIRED accepts, sign a $0 EIP-3009 authorization, and send the
    standard base64 v2 payload. Routing that into _settle_base_path would
    attempt a real CDP/JSON-RPC settlement of a $0 transfer, which always
    fails.

    There is no money to verify on a $0 challenge, so a well-formed v2 payload
    is the free proof: consume its EIP-3009 nonce for replay/dedup (atomic
    in-memory check-and-add + awaited insert-only record_tx_hash, same pattern
    as the paid paths) and hand back an auth dict. The payment_logs lifecycle
    is the same as the paid Base path. Nothing moves on-chain — the unsettled
    authorization simply expires (EIP-3009 validity ≤300s).

    Payer identity is self-reported (signature not recovered) — the same trust
    level as the SDK free flow's `from=` field. Priced tools are never routed
    here.
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
        # record_tx_hash returns None on infra error and the paid paths fail
        # closed on it. This path deliberately fails open (None falls
        # through): it is a $0 free proof, nothing of value can be replayed,
        # and bouncing free calls on a Supabase blip buys no security. The
        # in-memory nonce set still dedupes within the process.
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

    # Free calls have no pre-402 row, so their terminal write below is one
    # complete insert carrying the whole lifecycle.
    try:
        is_free_call = Decimal(str(tool.price_usdc or "0")) == 0
    except Exception:
        is_free_call = False

    try:
        gateway_fee = str(
            Decimal(tool.price_usdc) * Decimal(str(settings.GATEWAY_FEE_PERCENT))
        )
    except Exception:
        gateway_fee = None

    # Resolved name so legacy aliases credit the canonical tool.
    registry.increment_call_count(resolved)

    # Every paid settle inserts its own 'verified' row here — payment_id is
    # the challenge UUID on Stellar and the tx_hash on Base/Stacks — and the
    # terminal PATCH lands on it. The insert runs concurrently with tool
    # execution, but its task handle is awaited before any terminal state
    # write; otherwise the PATCH can race the insert, no-op on the missing
    # row, and leave it stuck in 'verified'.
    insert_task: Optional[asyncio.Task] = None
    if sb_enabled() and not is_free_call and not auth.get("redeemed"):
        insert_task = asyncio.create_task(insert_pending_payment_log(
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
            # Buyer-observability: the settled call's request params — which
            # symbols pre_trade_check screened, which need verified_route
            # vetted. This is the row the buyer-health digest reads.
            parameters=body.parameters or None,
        ))
        # Mark the 402 that prompted this payment 'superseded' instead of
        # letting the sweep call it 'abandoned'; otherwise every success also
        # books a phantom abandonment and the conversion ratio is wrong by
        # construction. Fire-and-forget on purpose: the payment already
        # settled on-chain, so analytics must not add latency or a failure
        # mode to it. If this misses, the row falls through to the sweep.
        # 402s no longer write pending rows, so this only matches legacy rows
        # and can be removed together with the abandoned sweep once they age
        # out.
        asyncio.create_task(correlate_pending_challenge(
            tool_name=resolved,
            client_ip=client_ip,
            user_agent=user_agent_str,
            tx_hash=tx_hash,
        ))

    async def _ensure_row_inserted():
        """Barrier before every terminal state write.
        A PATCH keyed on tx_hash must not run until the tx-keyed row exists.
        Insert failures are logged, not raised — the payment already settled
        on-chain, so bookkeeping must never fail the response."""
        if insert_task is not None:
            try:
                await insert_task
            except Exception as e:
                logger.warning(
                    f"[AGE-58] tx-keyed row insert failed for {payment_id[:16]}…: {e}"
                )

    try:
        tool_result = await _run_tool(tool, resolved, tool_name, body.parameters)
    except Exception as e:
        # _refund_and_500 PATCHes the row's state too — same ordering rule.
        await _ensure_row_inserted()
        return await _refund_and_500(tool_name, payment_id, e)

    # A paid tool that produced only an error refunds rather than charges.
    # real_tool_response swallows executor failures (missing implementation,
    # upstream API errors) into {"error": ...} with a 200 — harmless for a $0
    # tool, but a paid tool would charge the agent for nothing and leave the
    # payment in 'payment_done' with no refund state. "error" is the uniform
    # top-level failure marker across every executor; success shapes never
    # carry it.
    if (isinstance(tool_result, dict) and "error" in tool_result
            and Decimal(str(tool.price_usdc or "0")) > 0):
        await _ensure_row_inserted()
        return await _refund_and_500(
            tool_name, payment_id,
            RuntimeError(f"paid tool returned error: {tool_result['error']}"),
        )

    append_transaction({
        "tool": tool_name,
        "amount_usdc": tool.price_usdc,
        "agent": agent_address,
        "tx_hash": tx_hash,
        "success": True,
    })
    agent_log = (agent_address or "unknown")[:8]
    logger.info(f"[CALL] tool={tool_name} agent={agent_log}... status=completed tx={tx_hash}")

    # Terminal 'payment_done' write — awaited so analytics are consistent at
    # response time. The single Supabase write on the happy path. Barrier
    # first so the PATCH lands on the inserted row.
    await _ensure_row_inserted()
    if is_free_call:
        # Free path: no pending row exists (skipped at _issue_402), so the
        # terminal write is one complete row. Every settled call still lands
        # in payment_logs — the analytics-lifecycle invariant holds, in a
        # single round trip.
        if sb_enabled():
            await insert_pending_payment_log(
                payment_id=payment_id,
                tool_name=resolved,
                network=receipt_network,
                amount_usdc=tool.price_usdc,
                state="payment_done",
                agent_address=agent_address,
                tx_hash=tx_hash,
                developer_address=tool.developer_address or None,
                gateway_fee_usdc=gateway_fee,
                client_ip=client_ip,
                user_agent=user_agent_str,
                parameters=body.parameters or None,
            )
    else:
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

    # Echo the parameters the tool actually ran with, so a buyer whose intent
    # was dropped (or who forgot to send any) can see it in the response
    # instead of silently receiving a default-parameters verdict.
    params_used = body.parameters or {}
    response = {
        "tool": tool_name,
        "result": tool_result,
        "parameters_received": params_used,
        "payment": {
            "amount_usdc": tool.price_usdc,
            "tx_hash": auth.get("tx_hash"),
            "network": receipt_network,
        },
    }
    if not params_used:
        response["parameters_note"] = (
            "no parameters received — the tool ran on its defaults; send a "
            'JSON body with your arguments (top-level or under "parameters")'
        )
    related = _PAID_RELATED.get(resolved)
    if related:
        response["related"] = {"hint": _RELATED_HINT, "paid_tools": related}
    return response


@router.get("/tools/{tool_name}/call")
@limiter.limit("60/minute")
async def call_tool_get(tool_name: str, request: Request):
    """x402 discovery crawlers probe resources with GET — answer with the
    same 402 challenge POST issues, so the validation crawl can read the
    PAYMENT-REQUIRED header (incl. extensions.bazaar). Without this the
    crawl gets a 405 and the listing never leaves 'processing'.
    """
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = _apply_demo_pricing(registry.get_tool(resolved))
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    if not tool.active:
        raise HTTPException(status_code=503, detail=f"Tool '{tool_name}' is currently unavailable")
    # GET is the discovery-probe path: no pending row, or periodic health
    # checks would mint phantom pending→abandoned rows and a Supabase blip
    # would 503 crawler probes.
    return await _issue_402(
        tool, resolved, tool_name, ToolCallRequest(), request,
        None, f"{GATEWAY_URL}/tools/{tool_name}/call",
        log_pending=False,
    )


@router.post(
    "/tools/{tool_name}/call",
    # Body is read manually inside the handler — keep the schema visible in
    # OpenAPI/docs since FastAPI cannot infer it.
    openapi_extra={"requestBody": {
        "required": False,
        "content": {"application/json": {
            "schema": ToolCallRequest.model_json_schema()}},
    }},
)
@limiter.limit("100/minute")                                        # per-IP
@limiter.limit(settings.WALLET_RATE_LIMIT, key_func=wallet_or_ip)  # per-wallet
async def call_tool(
    tool_name: str,
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
      1. Neither header → _issue_402 (advertise both options) — the body is
         parsed leniently first: a bare/malformed POST still gets the 402,
         never a 422.
      2. X-Payment → _settle_stellar, then _execute_and_log
      3. PAYMENT-SIGNATURE + $0 tool → _settle_free_v2 (no on-chain settle)
      4. PAYMENT-SIGNATURE → _settle_base_path, then _execute_and_log
      (2–4 validate the body strictly before settling, so a malformed paid
      call 422s without burning the payment.)
    """
    resolved = _TOOL_ALIASES.get(tool_name, tool_name)
    tool = _apply_demo_pricing(registry.get_tool(resolved))
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    if not tool.active:
        raise HTTPException(status_code=503, detail=f"Tool '{tool_name}' is currently unavailable")

    resource_url = f"{GATEWAY_URL}/tools/{tool_name}/call"

    x_payment, payment_signature = normalize_payment_headers(x_payment, payment_signature)
    unpaid = not x_payment and not payment_signature

    body, body_err = await parse_body_after_payment_gate(
        request, ToolCallRequest, strict=not unpaid,
    )
    if body_err is not None:
        return body_err

    agent_address = x_agent_address or body.agent_address

    if unpaid:
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
        # ── Stacks dispatch: HTTP headers are case-insensitive, so
        # the lowercase dialect can't be routed on casing — route on the
        # payload's CAIP-2 network instead. Priced tools only: a stacks
        # payload on a $0 tool falls through to _settle_free_v2 (free proofs
        # never touch a chain).
        _ps_payload, _ps_err = stacks_pay.decode_payment_signature(payment_signature)
        _is_stacks = (
            not _is_free_tool
            and isinstance(_ps_payload, dict)
            and str(_ps_payload.get("network") or "").startswith("stacks")
        )
        if _is_stacks:
            auth = await _settle_stacks_path(tool, tool_name, payment_signature,
                                             _ps_payload, parameters=body.parameters)
            if isinstance(auth, JSONResponse):
                return auth
            # Verified payer = the tx's origin signer (c32) — same
            # verified-wins rule as the Base path.
            agent_address = auth["payer"] or agent_address
            payment_id = auth.get("tx_hash", "")
            is_base = True   # tx-keyed payment_logs row semantics
        elif _is_free_tool:
            # Standard v2 payload on a $0 tool — accept as the free proof,
            # never attempt a real settlement of $0. Nothing is verified on a
            # $0 call, so the declared address keeps priority.
            auth = await _settle_free_v2(tool_name, payment_signature)
            if isinstance(auth, JSONResponse):
                return auth
            agent_address = agent_address or auth["payer"]
        else:
            auth = await _settle_base_path(tool, tool_name, payment_signature, resource_url)
            if isinstance(auth, JSONResponse):
                return auth
            # The settle result's payer is verified (Mode A: CDP-attested
            # EIP-3009 signer; Mode B: bound to the Transfer log's from-topic).
            # The declared agent_address is not — buyers copy docs-example
            # addresses into the request. Verified payer wins; declared is
            # the fallback.
            agent_address = auth["payer"] or agent_address
        payment_id = auth.get("tx_hash", "")
        is_base = True

    return await _execute_and_log(
        tool, resolved, tool_name, body, request,
        auth, agent_address, payment_id, is_base,
    )


# Registration validation. The price cap leaves generous headroom over
# current registry prices without letting an injected tool demand
# meaningful money per call.
_REGISTER_NAME_RE      = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_REGISTER_STELLAR_RE   = re.compile(r"^G[A-Z2-7]{55}$")
_REGISTER_EVM_RE       = re.compile(r"^0x[0-9a-fA-F]{40}$")
_REGISTER_MAX_PRICE    = Decimal("1")
_REGISTER_CATEGORIES   = {"data", "defi", "trading", "monitoring", "security"}


def _validate_developer_address(addr: str) -> bool:
    """Stellar ed25519 public strkey (checksum-verified when stellar_sdk is
    importable, regex shape otherwise) or an EVM address."""
    if _REGISTER_EVM_RE.match(addr or ""):
        return True
    if not _REGISTER_STELLAR_RE.match(addr or ""):
        return False
    try:
        from stellar_sdk import StrKey
        return StrKey.is_valid_ed25519_public_key(addr)
    except ImportError:
        return True  # regex shape already checked


async def _validate_registration(body: RegisterToolRequest) -> Optional[str]:
    """Return an error string for invalid registrations, None when valid."""
    if not _REGISTER_NAME_RE.match(body.name or ""):
        return ("invalid name: must match ^[a-z][a-z0-9_]{2,39}$ "
                "(lowercase slug, 3-40 chars)")
    if not body.description or len(body.description) > 500:
        return "invalid description: required, max 500 chars"
    if body.category not in _REGISTER_CATEGORIES:
        return f"invalid category: must be one of {sorted(_REGISTER_CATEGORIES)}"
    try:
        price = Decimal(str(body.price_usdc))
        if not price.is_finite() or price < 0 or price > _REGISTER_MAX_PRICE:
            raise ValueError
    except Exception:
        return (f"invalid price_usdc: must be a decimal in "
                f"[0, {_REGISTER_MAX_PRICE}] USDC")
    if not _validate_developer_address(body.developer_address):
        return ("invalid developer_address: must be a Stellar public key "
                "(G...) or an EVM address (0x + 40 hex)")
    if not isinstance(body.parameters, dict) or len(str(body.parameters)) > 10_000:
        return "invalid parameters: must be a JSON object under 10KB"
    if not body.endpoint:
        return "invalid endpoint: required"
    safe, why = await asyncio.to_thread(_endpoint_is_safe, body.endpoint)
    if not safe:
        return f"invalid endpoint: {why}"
    return None


@router.post("/tools/register")
@limiter.limit("10/minute")
async def register_tool(
    body: RegisterToolRequest,
    request: Request,
    x_register_secret: Optional[str] = Header(None),
):
    """Register a new MCP tool in the marketplace.

    Registration is gated because a tool carries a developer_address (the
    revenue-split recipient) and an endpoint the gateway will call:
      - 404 when TOOL_REGISTER_SECRET is unset (registration off — there is
        no third-party developer flow yet)
      - 401 unless X-Register-Secret matches (constant-time compare)
      - 422 unless name/price/addresses/endpoint validate; endpoints must be
        https and resolve only to public addresses
    """
    secret = settings.TOOL_REGISTER_SECRET
    if not secret:
        raise HTTPException(status_code=404, detail="Not found")
    # Compare bytes inside try — a non-latin-1 header gets a clean 401, not
    # a TypeError 500.
    try:
        authorized = hmac.compare_digest(
            (x_register_secret or "").encode(), secret.encode()
        )
    except Exception:
        authorized = False
    if not authorized:
        raise HTTPException(status_code=401, detail="Unauthorized")

    err = await _validate_registration(body)
    if err:
        raise HTTPException(status_code=422, detail=err)

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
        logger.info(
            f"[REGISTER] tool={body.name} price={body.price_usdc} "
            f"dev={body.developer_address[:10]}... endpoint={body.endpoint}"
        )
        # Persist so the registration survives the next restart. The tool is
        # already live in-memory for this process, so a Supabase blip does
        # not fail the request; `persisted` tells the caller whether the
        # registration will outlive a redeploy.
        persisted = await persist_tool_registration(registry.tool_to_dict(tool))
        if not persisted:
            logger.warning(
                f"[REGISTER] tool={body.name} registered in-memory but NOT "
                f"persisted to Supabase — it will be lost on the next restart"
            )
        return {
            "status":    "registered",
            "persisted": persisted,
            "tool":      registry.tool_to_dict(tool),
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
