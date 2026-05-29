"""
routes/agent.py — Agent-native onboarding.

  POST /v1/agent/register — Mint a fresh wallet + session token in ONE call.
                            Free. No payment, no form, no human in the loop.

This is the top of AgentPay's Phase-1 funnel (see STRATEGY.md). An autonomous
agent can discover AgentPay, register, and immediately start calling the 17 free
tools — three API calls, zero human involvement:

    POST /v1/agent/register          → { wallet, session_token, ... }
    GET  /tools                      → list tools (free + paid)
    POST /tools/{name}/call          → { tool, result, payment }

The wallet gives the agent an economic identity from its first call — free tools
cost nothing, and when it later needs a paid tool it already holds a fundable
wallet. Stellar is the default (sub-cent fees, no gas-token trap; USDC is now
cross-chain via Circle CCTP), with Base available on request.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

import registry
from gateway._limiter import limiter
from gateway.config import GATEWAY_URL, settings

logger = logging.getLogger(__name__)
router = APIRouter()


class AgentRegisterRequest(BaseModel):
    label: Optional[str] = None          # optional human/agent-readable label
    network: str = "stellar"             # "stellar" (default) or "base"
    max_spend: str = "0.10"              # suggested default session budget cap


def _new_stellar_wallet() -> dict:
    from stellar_sdk import Keypair
    kp = Keypair.random()
    return {
        "network":    "stellar",
        "public_key": kp.public_key,
        "secret_key": kp.secret,        # the agent controls its own key
        "funded":     False,
    }


def _new_base_wallet() -> Optional[dict]:
    # eth_account is an optional dependency on the gateway; degrade cleanly.
    try:
        from eth_account import Account
    except ImportError:
        return None
    acct = Account.create()
    return {
        "network":     "base",
        "public_key":  acct.address,
        "secret_key":  "0x" + acct.key.hex(),   # agent controls its own key
        "funded":      False,
    }


@router.post("/v1/agent/register")
@limiter.limit("30/minute")
async def register_agent(body: AgentRegisterRequest, request: Request):
    """
    Mint a fresh wallet + session token. Free, no payment.

    Returns the wallet (the agent keeps its own secret), a session_token, the
    list of free tools it can call immediately, and pointers to the next two
    calls. Designed so an agent goes from zero to a free tool call without any
    human step.
    """
    network = (body.network or "stellar").lower()
    if network == "base":
        wallet = _new_base_wallet()
        if wallet is None:
            # Fall back to Stellar rather than fail — agent still gets identity.
            wallet = _new_stellar_wallet()
    else:
        wallet = _new_stellar_wallet()

    agent_id      = str(uuid.uuid4())
    session_token = str(uuid.uuid4())
    created_at    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Free tool catalog (price == 0) so the agent knows what it can call for free.
    try:
        free_tools = [
            t.name for t in registry.list_tools()
            if float(getattr(t, "price_usdc", "0") or "0") == 0 and getattr(t, "active", True)
        ]
    except Exception:
        free_tools = []

    logger.info(
        f"[REGISTER] agent_id={agent_id[:8]}... network={wallet['network']} "
        f"wallet={wallet['public_key'][:8]}... free_tools={len(free_tools)}"
    )

    return {
        "agent_id":      agent_id,
        "session_token": session_token,
        "wallet":        wallet,
        "max_spend":     body.max_spend,
        "label":         body.label,
        "gateway_url":   GATEWAY_URL,
        "tools_endpoint": f"{GATEWAY_URL}/tools",
        "free_tools":    free_tools,
        "created_at":    created_at,
        "next": {
            "list_tools": f"GET {GATEWAY_URL}/tools",
            "call_tool":  f"POST {GATEWAY_URL}/tools/{{name}}/call",
        },
        "notes": (
            "Free tools need no payment — call them now. Fund this wallet "
            "(USDC) only when you want a paid tool. Use `from agentpay import "
            "AgentWallet, Session` with this secret_key to enforce max_spend "
            "client-side."
        ),
    }
