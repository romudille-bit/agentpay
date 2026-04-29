"""
main.py — AgentPay Gateway Server

Wiring layer only. Application logic lives in:
  gateway/routes/    — HTTP handlers (FastAPI routers)
  gateway/services/  — payment lifecycle, cache, supabase, tool runtime
  gateway/x402.py    — x402 protocol primitives
  gateway/stellar.py — Stellar payment verification
  gateway/base.py    — Base/EVM payment verification
  gateway/config.py  — pydantic-settings + GATEWAY_URL fallback

Run with:
    uvicorn gateway.main:app --reload --port 8000
"""

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import registry
from registry import reload_tools

from gateway._limiter import limiter
from gateway.config import GATEWAY_URL, settings
from gateway.routes.discovery import router as discovery_router
from gateway.routes.faucet import router as faucet_router
from gateway.routes.infra import router as infra_router
from gateway.routes.tools import router as tools_router
from gateway.services.supabase import sb_enabled, sb_headers

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AgentPay Gateway",
    description="x402 payment gateway for MCP tools on Stellar",
    version="0.1.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Mount routers ─────────────────────────────────────────────────────────────
app.include_router(infra_router)
app.include_router(tools_router)
app.include_router(discovery_router)
app.include_router(faucet_router)


async def _keepalive_loop():
    """Ping /health every 5 minutes to prevent Railway cold-start."""
    await asyncio.sleep(60)  # wait for full startup before first ping
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{GATEWAY_URL}/health")
        except Exception:
            pass  # silent — keepalive is best-effort
        await asyncio.sleep(300)  # 5 minutes


@app.on_event("startup")
async def _startup():
    # Scheduling the keepalive task can be disabled (e.g. by the test suite)
    # so the background ping doesn't fire at the production URL during
    # local imports. Default behaviour is unchanged. Accepts the common
    # boolean idioms — "1", "true", "yes", "on" (case-insensitive) — so
    # nobody gets surprised by a literal-string mismatch.
    #
    # TODO(tier-2): the keepalive currently pings GATEWAY_URL — a hardcoded
    # production URL — even when the gateway is running locally or on
    # gateway-testnet, which is a wasteful round-trip through Railway's edge
    # back to the same worker. Switch to f"http://localhost:{settings.PORT}"
    # once we add a settings.LOCAL_KEEPALIVE flag.
    if os.environ.get("KEEPALIVE_DISABLED", "").lower() not in {"1", "true", "yes", "on"}:
        asyncio.create_task(_keepalive_loop())
    if not sb_enabled():
        logger.info("Supabase not configured — using in-memory registry")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/tools",
                headers=sb_headers(),
                params={"select": "*", "active": "eq.true"},
            )
        if resp.status_code != 200:
            logger.warning(f"Supabase tools fetch failed ({resp.status_code}) — using in-memory registry")
            return
        rows = resp.json()
        if not rows:
            logger.warning("Supabase tools table empty — using in-memory registry")
            return
        from registry import Tool
        tools = [
            Tool(
                name=r["name"],
                description=r.get("description", ""),
                endpoint=r.get("endpoint", ""),
                price_usdc=r.get("price_usdc", "0"),
                developer_address=r.get("developer_address", ""),
                parameters=r.get("parameters", {}),
                category=r.get("category", "data"),
                active=r.get("active", True),
                uptime_pct=r.get("uptime_pct", 100.0),
                total_calls=r.get("total_calls", 0),
                triggers=r.get("triggers", []),
                use_when=r.get("use_when", ""),
                returns=r.get("returns", ""),
                response_example=r.get("response_example"),
            )
            for r in rows
        ]
        # Merge response_example from seed registry for any tools missing it in Supabase
        # _TOOLS isn't re-exported from registry/__init__.py — import it
        # directly from the submodule. Previously this raised ImportError
        # on every Railway deploy and got caught by the broad except below,
        # which logged the misleading "Supabase unavailable" warning even
        # though Supabase had just returned 200.
        from registry.registry import _TOOLS as _SEED
        for t in tools:
            if t.response_example is None and t.name in _SEED:
                t.response_example = _SEED[t.name].response_example
        reload_tools(tools)
        logger.info(f"Loaded {len(tools)} tools from Supabase")
    except Exception as e:
        logger.warning(f"Supabase unavailable ({e}) — using in-memory registry")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
