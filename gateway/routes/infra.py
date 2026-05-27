"""
routes/infra.py — Basic gateway-status endpoints.

  GET / | HEAD /     — landing page (HTML) for browsers, JSON manifest for agents
  GET /health        — Railway healthcheck target
  GET /stats         — pending payments + recent transaction tail
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

import registry

from gateway.config import GATEWAY_URL, settings
from gateway.landing import render_landing
from gateway.services.transaction_log import recent_transactions
from gateway.x402 import get_pending_count

router = APIRouter()


@router.api_route("/", methods=["GET", "HEAD"])
async def root(request: Request):
    """Content-negotiated root.

    Browsers (Accept: text/html) get the landing page. Agents and API clients
    get the JSON manifest. HEAD requests run GET and drop the body, so both
    content types respond with 200 — keeps the Bazaar "quality score" check
    happy regardless of which Accept the indexer sends.
    """
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return HTMLResponse(content=render_landing(registry.list_tools(), GATEWAY_URL))

    return JSONResponse(content={
        "name":             "AgentPay",
        "tagline":          "Your agent is only as smart as its data",
        "version":          "1.0",
        "tools":            len(registry.list_tools()),
        "docs":             "https://github.com/romudille-bit/agentpay",
        "tools_endpoint":   f"{GATEWAY_URL}/tools",
        "faucet":           f"{GATEWAY_URL}/faucet",
        "discovery":        f"{GATEWAY_URL}/.well-known/agentpay.json",
        "payment_networks": ["stellar", "base"],
    })


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "network": settings.STELLAR_NETWORK,
        "gateway_address": settings.GATEWAY_PUBLIC_KEY or "NOT_CONFIGURED",
        "pending_payments": get_pending_count(),
    }


@router.get("/stats")
async def stats():
    """Gateway statistics."""
    tools = registry.list_tools()
    total_calls = sum(t.total_calls for t in tools)
    return {
        "total_tools": len(tools),
        "total_calls": total_calls,
        "recent_transactions": recent_transactions(10),
        "pending_payments": get_pending_count(),
        "network": settings.STELLAR_NETWORK,
    }
