"""
routes/infra.py — Basic gateway-status endpoints.

  GET / | HEAD /     — landing page (HTML) for browsers, JSON manifest for agents
  GET /health        — Railway healthcheck target
  GET /stats         — pending payments + recent transaction tail
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

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
        "tagline":          "Economic intelligence for autonomous agents",
        "version":          "1.0",
        "tools":            len(registry.list_tools()),
        "docs":             "https://github.com/romudille-bit/agentpay",
        "tools_endpoint":   f"{GATEWAY_URL}/tools",
        "faucet":           f"{GATEWAY_URL}/faucet",
        "discovery":        f"{GATEWAY_URL}/.well-known/agentpay.json",
        "payment_networks": ["stellar", "base"],
    })


_FAVICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <!-- dark rounded background -->
  <rect width="32" height="32" rx="7" fill="#0a0a0b"/>
  <!-- teal "A" mark — two legs + crossbar -->
  <path d="M16 5 L26 27 H22 L19.5 21 H12.5 L10 27 H6 Z M14 17 H18 L16 12 Z"
        fill="#5eead4"/>
</svg>
"""


@router.get("/favicon.svg", response_class=Response)
async def favicon():
    """SVG favicon — dark background, teal A mark."""
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


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
