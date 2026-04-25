"""
routes/infra.py — Basic gateway-status endpoints.

  GET / | HEAD /     — name, version, tool count, key URLs
  GET /health        — Railway healthcheck target
  GET /stats         — pending payments + recent transaction tail
"""

from fastapi import APIRouter

import registry

from gateway.config import settings
from gateway.services.transaction_log import recent_transactions
from gateway.x402 import get_pending_count

router = APIRouter()


@router.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {
        "name":             "AgentPay",
        "tagline":          "Your agent is only as smart as its data",
        "version":          "1.0",
        "tools":            len(registry.list_tools()),
        "docs":             "https://github.com/romudille-bit/agentpay",
        "tools_endpoint":   "https://gateway-production-2cc2.up.railway.app/tools",
        "faucet":           "https://gateway-production-2cc2.up.railway.app/faucet",
        "discovery":        "https://gateway-production-2cc2.up.railway.app/.well-known/agentpay.json",
        "payment_networks": ["stellar", "base"],
    }


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
