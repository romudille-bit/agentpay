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
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import registry
from registry import reload_tools

from gateway._limiter import limiter
from gateway.config import GATEWAY_URL, settings
from gateway.routes.agent import router as agent_router
from gateway.routes.discovery import router as discovery_router
from gateway.routes.faucet import router as faucet_router
from gateway.routes.infra import router as infra_router
from gateway.routes.session import router as session_router
from gateway.routes.tools import router as tools_router
from gateway.services import supabase as sb
from gateway.services.supabase import sb_enabled, sb_headers

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def _keepalive_loop():
    """Ping /health every 5 minutes to prevent Railway cold-start.

    Pings localhost — the point is to keep THIS worker's process warm, and
    a local ping avoids a wasteful round-trip through Railway's edge (and
    avoids local/testnet instances pinging the production URL).
    """
    await asyncio.sleep(60)  # wait for full startup before first ping
    # KEEPALIVE_URL env override: set to the public URL if Railway app
    # sleeping (edge-traffic based) ever gets enabled on the service.
    target = settings.KEEPALIVE_URL or f"http://localhost:{settings.PORT}/health"
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(target)
        except Exception:
            pass  # silent — keepalive is best-effort
        await asyncio.sleep(300)  # 5 minutes


# How often the cleanup task runs in the background (PR #13e cutover).
# 10min is the sweet spot: pending_challenges have a 120s TTL, so worst-
# case a row is ~12min stale before sweep. cleanup_expired_challenges()
# deletes anything > 1h past expiry, so a real backlog needs the gateway
# to be down for 50+ minutes before a row qualifies.
_CLEANUP_INTERVAL_SECS = 600


async def _cleanup_loop():
    """Periodic sweep of expired pending_challenges in Supabase.

    Pairs with sb.cleanup_expired_challenges (which DELETEs rows where
    expires_at < now() - 1h). Without this loop, pending_challenges would
    grow indefinitely from abandoned 402 calls — agents that get a
    challenge and never pay. Cleanup runs ~every 10 minutes.

    No-op if Supabase isn't configured. Errors are swallowed since this
    is a best-effort background task; a single failed sweep doesn't
    matter, the next one will catch up.
    """
    # Wait past first hydration before sweeping; no need to race startup.
    await asyncio.sleep(_CLEANUP_INTERVAL_SECS)
    while True:
        try:
            n = await sb.cleanup_expired_challenges()
            if n:
                logger.info(f"cleanup_expired_challenges swept {n} stale rows")
        except Exception as e:
            logger.warning(f"cleanup_expired_challenges failed: {e}")
        await asyncio.sleep(_CLEANUP_INTERVAL_SECS)


# How often the abandoned-pending sweep runs (PR #14).
# 5 min matches the 5-min cutoff in sb.sweep_abandoned_pending, so
# worst case a stuck pending row spends ~10 min before transitioning
# to 'abandoned'.
_ABANDONED_SWEEP_INTERVAL_SECS = 300


# How often the refund worker runs (PR #12).
# 60s is a balance between: tight enough that refunds feel ~real-time
# from the agent SDK's perspective (refund_eta_seconds=60), loose enough
# that 5 attempts × 60s = 5 minutes total retry budget per row before
# refund_failed — long enough for transient Horizon blips but short
# enough that bad rows don't pile up.
_REFUND_WORKER_INTERVAL_SECS = 60


async def _refund_worker_loop():
    """Periodic on-chain refund worker (PR #12 — Option C, capstone).

    Gated by REFUND_ENABLED. When the flag is False the loop isn't
    scheduled — refund_pending rows accumulate as analytics-only.
    When True the loop runs every 60s:

      1. claim_refund_pending(limit=20) — fetch oldest pending rows
      2. for each: lazy-import stellar.send_refund, increment attempt
         counter, send the refund, transition state.

    Failure handling:
      - send_refund returns success=False → leave row in refund_pending
        (refund_attempts now ≥ 1), next sweep picks it up again
      - 5th failure → mark_refund_failed with reason from last attempt
      - Base-network rows → mark_refund_failed immediately with
        'base_refund_not_implemented' (outgoing Base txs not built yet)

    Idempotency notes:
      - The refund is at the on-chain level (USDC transfer); duplicates
        would mean the agent receives 2× refund. The state-guard on
        mark_refund_done prevents the row's PATCH from running twice,
        but doesn't prevent a duplicate Stellar tx if the worker
        crashes after submit but before PATCH. Acceptable risk —
        single-pod deploy, low blast radius. Multi-pod scaling would
        need a claim/lock column.
    """
    # Lazy import to avoid coupling: services.supabase doesn't import
    # stellar, but main.py imports both — direct top-level import here
    # would order-couple them. Mirrors the lazy import in
    # stellar.py:split_payment.
    from gateway.services import supabase as sb
    from gateway.stellar import send_refund

    await asyncio.sleep(_REFUND_WORKER_INTERVAL_SECS)
    while True:
        try:
            rows = await sb.claim_refund_pending(limit=20)
            for row in rows:
                payment_id    = row.get("payment_id", "")
                agent_address = row.get("agent_address", "")
                amount_usdc   = str(row.get("amount_usdc", "0"))
                network       = row.get("network", "")
                attempts_so_far = int(row.get("refund_attempts", 0))

                # Defensive: skip malformed rows that survived to here
                if not (payment_id and agent_address and amount_usdc):
                    continue

                # Base refunds aren't implemented; short-circuit to
                # refund_failed instead of looping forever on rows
                # that can never succeed with the current code.
                if network.startswith("base-"):
                    await sb.mark_refund_failed(
                        payment_id,
                        "base_refund_not_implemented",
                    )
                    continue

                # Increment attempt count BEFORE the send so a worker
                # crash mid-attempt still counts towards the cap.
                await sb.increment_refund_attempt(payment_id)
                this_attempt = attempts_so_far + 1

                result = await send_refund(
                    agent_address=agent_address,
                    amount_usdc=amount_usdc,
                    payment_id=payment_id,
                )
                if result.get("success"):
                    await sb.mark_refund_done(
                        payment_id, result.get("tx_hash", ""),
                    )
                    logger.info(
                        f"[REFUND] payment_id={payment_id[:8]}... → refund_done "
                        f"tx={result.get('tx_hash', '')[:16]}..."
                    )
                elif this_attempt >= sb._REFUND_ATTEMPT_CAP:
                    reason = f"max_attempts:{result.get('reason', 'unknown')}"
                    await sb.mark_refund_failed(payment_id, reason)
                    logger.warning(
                        f"[REFUND] payment_id={payment_id[:8]}... → refund_failed "
                        f"after {this_attempt} attempts ({reason})"
                    )
                else:
                    logger.info(
                        f"[REFUND] payment_id={payment_id[:8]}... attempt "
                        f"{this_attempt}/{sb._REFUND_ATTEMPT_CAP} failed "
                        f"({result.get('reason', 'unknown')}); will retry"
                    )
        except Exception as e:
            logger.warning(f"refund worker loop iteration failed: {e}")
        await asyncio.sleep(_REFUND_WORKER_INTERVAL_SECS)


async def _abandoned_sweep_loop():
    """Periodic PATCH pending → abandoned on payment_logs (PR #14).

    Pairs with sb.sweep_abandoned_pending (which UPDATEs rows where
    state='pending' AND created_at < now() - interval '5 min'). Without
    this loop, every 402 challenge that never gets paid leaves a row
    stuck in 'pending' forever, breaking the conversion analytics query
    in §5.5 of the design doc.

    Different table from _cleanup_loop:
      _cleanup_loop          → DELETE from pending_challenges (transient lookup)
      _abandoned_sweep_loop  → PATCH payment_logs (permanent audit trail)

    No-op if Supabase isn't configured.
    """
    await asyncio.sleep(_ABANDONED_SWEEP_INTERVAL_SECS)
    while True:
        try:
            n = await sb.sweep_abandoned_pending()
            if n:
                logger.info(f"sweep_abandoned_pending: {n} rows → abandoned")
        except Exception as e:
            logger.warning(f"sweep_abandoned_pending failed: {e}")
        await asyncio.sleep(_ABANDONED_SWEEP_INTERVAL_SECS)


async def _hydrate_replay_state_from_supabase() -> None:
    """Warm the in-memory replay caches from Supabase at startup.

    After the PR #13e cutover Supabase is primary, but the in-memory
    sets/dicts stay as a graceful-degradation cache: if Supabase goes
    down mid-operation, reads fall back to these structures. Without
    hydration the cache is cold on every redeploy — a Supabase outage
    immediately after a deploy would mean replay protection silently
    falls open. Hydration closes that window.

    Pulls bounded recent rows (avoiding a full scan of all-time replay
    data):
      - replay_payment_ids: last hour, all UUIDs → _completed_payments
        (Stellar in-memory dedupe is keyed on tx_hash but we add the
        payment_id too as a defense in depth — won't false-positive
        because UUIDs and Stellar tx hashes have different shapes)
      - replay_tx_hashes: last hour → _completed_payments (Stellar)
        and _used_base_tx_hashes (Base) keyed on network prefix
      - pending_challenges: live (expires_at > now()) → _pending_challenges
        with the same shape verify_and_fulfill expects

    No-op if Supabase isn't configured. Errors are logged but don't
    block startup — the gateway must always come up.
    """
    # Lazy imports so we don't create module-level cycles
    from gateway.x402 import (
        _completed_payments,
        _pending_challenges,
        _normalize_supabase_challenge,
    )
    from gateway.base import _used_base_tx_hashes
    from datetime import datetime, timedelta, timezone

    hour_ago_iso = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # replay_payment_ids — last hour
            r = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/replay_payment_ids",
                headers=sb_headers(),
                params={"select": "payment_id", "consumed_at": f"gt.{hour_ago_iso}"},
            )
            if r.status_code == 200:
                for row in r.json():
                    pid = row.get("payment_id")
                    if pid:
                        _completed_payments.add(pid)

            # replay_tx_hashes — last hour, split by network prefix into
            # the two in-memory dedupe stores
            r = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/replay_tx_hashes",
                headers=sb_headers(),
                params={"select": "tx_hash,network", "consumed_at": f"gt.{hour_ago_iso}"},
            )
            if r.status_code == 200:
                for row in r.json():
                    tx = row.get("tx_hash")
                    net = (row.get("network") or "")
                    if not tx:
                        continue
                    if net.startswith("base-"):
                        _used_base_tx_hashes.add(tx)
                    else:
                        # stellar-mainnet / stellar-testnet → _completed_payments
                        # (also covers unknown prefixes defensively)
                        _completed_payments.add(tx)

            # pending_challenges — non-expired only
            r = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/pending_challenges",
                headers=sb_headers(),
                params={"select": "*", "expires_at": f"gt.{now_iso}"},
            )
            if r.status_code == 200:
                for row in r.json():
                    pid = row.get("payment_id")
                    if pid:
                        _pending_challenges[pid] = _normalize_supabase_challenge(row)

        logger.info(
            f"Replay hydration: "
            f"{len(_completed_payments)} payment_ids/tx_hashes, "
            f"{len(_used_base_tx_hashes)} base tx_hashes, "
            f"{len(_pending_challenges)} live challenges"
        )
    except Exception as e:
        logger.warning(f"Replay hydration from Supabase failed: {e}")


async def _hydrate_tools_from_supabase() -> None:
    """Pull active tool rows from Supabase and merge them onto the seed
    registry. Called from the lifespan startup hook.

    Falls back silently to the in-memory registry if Supabase is unreachable
    or returns an empty result — the gateway must always boot, even if the
    metadata source is down.
    """
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
        # Supabase rows can be partial (4 newer tools — yield_scanner,
        # funding_rates, open_interest, orderbook_depth — were inserted
        # without triggers/use_when/returns and never backfilled). Fall
        # back to the in-memory seed for any discovery field the Supabase
        # row left empty. This makes registry.py the source of truth for
        # discovery hints; Supabase becomes an override layer.
        #
        # NOTE: this means an *intentionally* empty value in Supabase
        # (e.g. triggers=[]) gets shadowed by the seed. We've never used
        # Supabase to deliberately clear fields, so this is fine in
        # practice — but worth knowing if that ever changes.
        sb_names = {t.name for t in tools}
        for t in tools:
            if t.name not in _SEED:
                continue
            seed = _SEED[t.name]
            # Always use seed price — Supabase may be stale after a pricing change
            t.price_usdc = seed.price_usdc
            if t.response_example is None: t.response_example = seed.response_example
            if not t.triggers:              t.triggers = seed.triggers
            if not t.use_when:              t.use_when = seed.use_when
            if not t.returns:               t.returns = seed.returns
        # Seed tools missing from Supabase (e.g. registry.py added a new tool
        # but Supabase hasn't been migrated yet). Registry.py is always the
        # source of truth for existence; Supabase is an override layer only.
        for name, seed in _SEED.items():
            if name not in sb_names and seed.active:
                tools.append(seed)
                logger.info(f"Tool '{name}' not in Supabase — using seed value")
        reload_tools(tools)
        logger.info(f"Loaded {len(tools)} tools ({len(sb_names)} from Supabase, {len(tools) - len(sb_names)} from seed)")
    except Exception as e:
        logger.warning(f"Supabase unavailable ({e}) — using in-memory registry")


def _validate_config() -> None:
    """Fail fast on a misconfigured mainnet deploy instead of at first payment.

    STELLAR_NETWORK defaults to testnet, so a missing env file silently
    produces a testnet gateway — but a deliberate mainnet deploy without
    wallet keys would advertise an empty pay_to on every 402 and fail every
    split/refund. Refuse to boot instead.
    """
    if settings.STELLAR_NETWORK != "mainnet":
        return
    missing = [k for k in ("GATEWAY_PUBLIC_KEY", "GATEWAY_SECRET_KEY")
               if not getattr(settings, k)]
    if missing:
        raise RuntimeError(
            f"Mainnet gateway requires {', '.join(missing)} — "
            f"set them in the environment or switch STELLAR_NETWORK to testnet."
        )


def _log_config_banner() -> None:
    """One-line resolved-config banner so a wrong-network deploy is obvious."""
    logger.info(
        "[CONFIG] stellar=%s wallet=%s base=%s supabase=%s refunds=%s facilitator=%s",
        settings.STELLAR_NETWORK,
        (settings.GATEWAY_PUBLIC_KEY[:8] + "...") if settings.GATEWAY_PUBLIC_KEY else "UNSET",
        settings.BASE_NETWORK if settings.BASE_GATEWAY_ADDRESS else "disabled",
        "on" if sb_enabled() else "off",
        "on" if settings.REFUND_ENABLED else "off",
        "on" if settings.STELLAR_FACILITATOR_ENABLED else "off",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler — replaces the deprecated
    @app.on_event("startup") / "shutdown" callbacks (PR #16 surfaced the
    deprecation warning). Same logic as before, just packaged in a single
    asynccontextmanager so we can hang shutdown drains off the post-yield
    half later (currently a no-op).

    Startup hooks (in order):
      1. Background keepalive ping (skipped if KEEPALIVE_DISABLED is set).
      2. Hydrate tool registry from Supabase (skipped if not configured).

    Shutdown hooks: none yet. Background tasks (#13 cutover row 7) will
    drain here so cleanup_expired_challenges() finishes before the worker
    exits.
    """
    # ── startup ──────────────────────────────────────────────────────────────
    _validate_config()
    _log_config_banner()

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
    else:
        await _hydrate_tools_from_supabase()
        # PR #13e cutover: warm the replay caches and start the periodic
        # pending_challenges sweep. Both are best-effort and can't block
        # startup.
        await _hydrate_replay_state_from_supabase()
        asyncio.create_task(_cleanup_loop())
        # PR #14: periodic pending → abandoned sweep on payment_logs.
        # Distinct from _cleanup_loop (different table, different
        # semantics — see _abandoned_sweep_loop docstring).
        asyncio.create_task(_abandoned_sweep_loop())

        # PR #12: async on-chain refund worker, gated by REFUND_ENABLED.
        # Picks up refund_pending rows, sends USDC back to the agent
        # on Stellar, transitions to refund_done or refund_failed.
        # Dark-launched at False by default so the state tracking can
        # soak before committing to actual refund spend.
        if settings.REFUND_ENABLED:
            logger.info("REFUND_ENABLED=true — starting refund worker loop")
            asyncio.create_task(_refund_worker_loop())
        else:
            logger.info("REFUND_ENABLED=false — refund worker disabled (dark launch)")

    yield

    # ── shutdown ─────────────────────────────────────────────────────────────
    # Background tasks (_keepalive_loop, _cleanup_loop) are daemon-style
    # — the runtime cancels them on worker exit. If we ever add a
    # split_payment retry queue with at-least-once semantics, this is
    # where the drain would go.


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AgentPay Gateway",
    description="The economic-intelligence layer for AI agents — budget-capped x402 spending sessions with verifiable receipts. USDC on Base (and Stellar).",
    version="0.1.0",
    lifespan=lifespan,
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
app.include_router(session_router)
app.include_router(discovery_router)
app.include_router(faucet_router)
app.include_router(agent_router)


# ── OpenAPI: mark non-paid routes as free so x402 indexers skip them ──────────
# x402 directories (x402scan, Bazaar) crawl /openapi.json and probe every
# operation expecting a 402. Only the paid session resource should be probed;
# every other route is free/utility. Tagging them `security: []` tells indexers
# "not x402-paid — don't probe," producing a clean listing with zero errors.
_PAID_PATHS = {"/v1/session/create"}


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    for path, operations in schema.get("paths", {}).items():
        if path in _PAID_PATHS:
            continue
        for method, op in operations.items():
            if method.lower() in ("get", "post", "put", "patch", "delete", "head", "options") and isinstance(op, dict):
                op["security"] = []
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
