"""
leg_verifier.py — chain-verify the paid legs of receipt-derived ledger runs
(AGE-142).

Why
---
/ledger renders two kinds of run. Runs clustered from `payment_logs` are
gateway-settled and fully verified. Runs rebuilt from an agent-posted SDK
receipt (the prober's probe_sweeps, the strategy run's direct CMC legs) can
only be verified against `payment_logs` too — and their legs settle
agent→seller, never touching our books — so every one of them rendered as
`agent_attested`: ~79% of the flagship's lifetime spend had no on-chain
evidence attached, on a page whose whole point is "verifiable receipts".

The evidence exists on Base regardless of who submitted the tx: an EIP-3009
`transferWithAuthorization` emits a standard USDC `Transfer` event FROM our
wallet. So for each receipt-derived run we pull the run wallet's outbound USDC
transfers in the run window (public JSON-RPC `eth_getLogs`, no key — the same
path `tools/reconcile_prober_spend.py` / AGE-88 already uses) and match them
to the receipt's paid legs. Results are cached in `ledger_leg_verifications`
so the ledger render never touches the chain (disk-IO rule: batch, not
request-time).

Matching, strongest first (pure function, unit-tested):
  hash          leg carries a tx_hash and that tx is among the wallet's
                outbound transfers in the window
  amount+payto  no hash; a transfer with the same amount to the seller's
                known payTo (hint from service_probes by resource_url)
  amount        a same-amount transfer from the run wallet in the window,
                by elimination (the wallet DID pay this amount on-chain in
                the window; attribution to this leg is positional)
Every match CONSUMES the transfer, so one settlement backs at most one leg.
Legs with no match stay agent_attested — never force-matched.

The label on /ledger is three-way and never collapsed:
  onchain          gateway-settled (payment_logs)      — we settled it
  onchain_chain    chain-verified (this module)        — we observed it
  agent_attested   no evidence found                   — reported only
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

import httpx

from gateway.config import settings

logger = logging.getLogger(__name__)

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BASE_BLOCK_SECONDS = 2
# A prober sweep settles its legs over the ~1-2h it runs; run_at is the sweep
# START. Strategy runs settle within minutes. Window = run_at - lead … + lag.
WINDOW_LEAD = timedelta(minutes=15)
WINDOW_LAG = timedelta(hours=3)
VERIFY_INTERVAL_SECONDS = 6 * 3600
VERIFY_RUNS_PER_CYCLE = 12
RECHECK_AFTER = timedelta(hours=36)     # re-check runs that had no evidence yet
MARKER_LEG = -1


# ── pure: matching ──────────────────────────────────────────────────────────

def _atomic(cost) -> Optional[int]:
    """'$0.0075' / '0.01' / Decimal → micro-USDC int, or None."""
    try:
        return int(Decimal(str(cost).strip().lstrip("$")) * 1_000_000)
    except (InvalidOperation, ValueError, TypeError):
        return None


def paid_legs(breakdown: Iterable[dict]) -> list[tuple[int, dict]]:
    """(1-based leg index, entry) for every receipt leg with cost > 0 — the
    same indexing `_run_view_from_breakdown` uses for `step`."""
    out = []
    for i, e in enumerate(breakdown, start=1):
        a = _atomic(e.get("cost"))
        if a and a > 0:
            out.append((i, e))
    return out


def match_legs(breakdown: Iterable[dict], transfers: Iterable[dict],
               payto_hints: Optional[dict[str, str]] = None) -> list[dict]:
    """Match receipt paid legs to the wallet's outbound USDC transfers.

    breakdown:   SDK receipt rows — {"tool" (url or name), "cost", "tx_hash"?}
    transfers:   {"to", "value" (atomic str/int), "hash", "timeStamp"?} —
                 already filtered to the run window and the run wallet
    payto_hints: {resource_url_or_tool: payto_lower} from service_probes

    Returns one row per MATCHED leg: {"leg_index", "tx_hash", "to",
    "amount_usdc", "method"}. Transfers are consumed on match. PURE."""
    pool: list[dict] = []
    for t in transfers:
        try:
            pool.append({**t, "_value": int(Decimal(str(t.get("value")))),
                         "_to": str(t.get("to") or "").lower(),
                         "_hash": str(t.get("hash") or "").lower()})
        except (InvalidOperation, ValueError, TypeError):
            continue
    hints = {str(k).lower(): str(v).lower() for k, v in (payto_hints or {}).items()}
    legs = paid_legs(breakdown)
    out: list[dict] = []
    matched: set[int] = set()

    def _take(pred) -> Optional[dict]:
        for i, t in enumerate(pool):
            if pred(t):
                return pool.pop(i)
        return None

    # Pass 1 — by hash (strongest).
    for idx, e in legs:
        tx = str(e.get("tx_hash") or "").lower()
        if not tx:
            continue
        t = _take(lambda t: t["_hash"] == tx)
        if t:
            out.append(_row(idx, t, "hash"))
            matched.add(idx)
    # Pass 2 — by amount + known payTo.
    for idx, e in legs:
        if idx in matched:
            continue
        amt = _atomic(e.get("cost"))
        payto = hints.get(str(e.get("tool") or "").lower())
        if not payto:
            continue
        t = _take(lambda t: t["_value"] == amt and t["_to"] == payto)
        if t:
            out.append(_row(idx, t, "amount+payto"))
            matched.add(idx)
    # Pass 3 — by amount alone, in leg order.
    for idx, e in legs:
        if idx in matched:
            continue
        amt = _atomic(e.get("cost"))
        t = _take(lambda t: t["_value"] == amt)
        if t:
            out.append(_row(idx, t, "amount"))
            matched.add(idx)
    out.sort(key=lambda r: r["leg_index"])
    return out


def _row(idx: int, t: dict, method: str) -> dict:
    return {
        "leg_index": idx,
        "tx_hash": t["_hash"],
        "to": t["_to"],
        "amount_usdc": f"{Decimal(t['_value']) / Decimal(1_000_000):f}",
        "method": method,
    }


# ── I/O: Base JSON-RPC ─────────────────────────────────────────────────────

async def _rpc(client: httpx.AsyncClient, method: str, params: list):
    resp = await client.post(
        settings.BASE_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Content-Type": "application/json",
                 "User-Agent": "agentpay-ledger-verifier/1.0 (+https://agentpay.tools/ledger)"},
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"rpc {method}: {data['error']}")
    return data["result"]


async def _block_ts(client: httpx.AsyncClient, n: int, cache: dict) -> int:
    if n not in cache:
        blk = await _rpc(client, "eth_getBlockByNumber", [hex(n), False])
        cache[n] = int(blk["timestamp"], 16)
    return cache[n]


async def wallet_transfers(wallet: str, start: datetime, end: datetime,
                           client: Optional[httpx.AsyncClient] = None) -> list[dict]:
    """Outbound USDC Transfer events FROM `wallet` on Base between start and
    end. Block range estimated from Base's 2s block time with a margin, then
    each log's block timestamp checked exactly. Shape matches match_legs()."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        cache: dict = {}
        latest = int(await _rpc(client, "eth_blockNumber", []), 16)
        latest_ts = await _block_ts(client, latest, cache)

        def est(ts: float) -> int:
            return latest - int((latest_ts - ts) // BASE_BLOCK_SECONDS)

        from_block = max(0, est(start.timestamp()) - 300)
        to_block = min(latest, est(end.timestamp()) + 300)
        logs = await _rpc(client, "eth_getLogs", [{
            "address": USDC_BASE,
            "fromBlock": hex(from_block), "toBlock": hex(to_block),
            "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + wallet[2:].lower()],
        }])
        out = []
        for log in logs:
            ts = await _block_ts(client, int(log["blockNumber"], 16), cache)
            t = datetime.fromtimestamp(ts, tz=timezone.utc)
            if not (start <= t <= end):
                continue
            out.append({
                "to": "0x" + log["topics"][2][-40:],
                "value": str(int(log["data"], 16)),
                "hash": log["transactionHash"],
                "timeStamp": str(ts),
            })
        return out
    finally:
        if own:
            await client.aclose()


# ── orchestration ───────────────────────────────────────────────────────────

def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def run_key(value) -> str:
    """Canonical key for a run_at timestamp: PostgREST emits variable
    fractional digits, so compare parsed datetimes, not strings."""
    d = _parse_ts(value)
    return d.astimezone(timezone.utc).isoformat() if d else str(value or "")


def run_wallet(meta: dict, fallbacks: Iterable[str]) -> Optional[str]:
    """The Base wallet that paid this run: the meta's own `wallet` if it's an
    EVM address, else the first EVM address in the ledger allowlist."""
    w = str(meta.get("wallet") or "")
    if w.startswith("0x") and len(w) == 42:
        return w
    for a in fallbacks:
        if str(a).startswith("0x") and len(str(a)) == 42:
            return str(a)
    return None


async def verify_run(meta: dict, fallbacks: Iterable[str],
                     payto_hints: Optional[dict[str, str]] = None,
                     client: Optional[httpx.AsyncClient] = None) -> list[dict]:
    """Verify one flagship_runs meta. Returns the rows to cache (matched legs
    + the marker row). [] if the run has no paid legs or no usable wallet."""
    run_at = _parse_ts(meta.get("run_at"))
    breakdown = (meta.get("receipt") or {}).get("breakdown") or []
    if run_at is None or not paid_legs(breakdown):
        return []
    wallet = run_wallet(meta, fallbacks)
    if not wallet:
        return []
    transfers = await wallet_transfers(wallet, run_at - WINDOW_LEAD,
                                       run_at + WINDOW_LAG, client=client)
    matches = match_legs(breakdown, transfers, payto_hints)
    now = datetime.now(timezone.utc).isoformat()
    rows = [{
        "run_at": meta.get("run_at"), "leg_index": m["leg_index"],
        "tx_hash": m["tx_hash"], "to_addr": m["to"],
        "amount_usdc": m["amount_usdc"], "wallet": wallet.lower(),
        "network": "base", "method": m["method"], "verified_at": now,
    } for m in matches]
    rows.append({
        "run_at": meta.get("run_at"), "leg_index": MARKER_LEG,
        "tx_hash": None, "to_addr": None, "amount_usdc": None,
        "wallet": wallet.lower(), "network": "base", "method": "checked",
        "verified_at": now,
    })
    return rows


def needs_check(meta: dict, existing: dict[tuple[str, int], dict],
                now: Optional[datetime] = None) -> bool:
    """A run needs (re)checking if it has paid legs and either was never
    checked, or was checked >RECHECK_AFTER ago with at least one paid leg
    still unmatched (transfers can lag a very late settle)."""
    breakdown = (meta.get("receipt") or {}).get("breakdown") or []
    legs = paid_legs(breakdown)
    if not legs:
        return False
    key = run_key(meta.get("run_at"))
    marker = existing.get((key, MARKER_LEG))
    if marker is None:
        return True
    if all((key, i) in existing for i, _ in legs):
        return False
    checked = _parse_ts(marker.get("verified_at"))
    now = now or datetime.now(timezone.utc)
    return bool(checked) and (now - checked) > RECHECK_AFTER


async def verify_pending(limit: int = VERIFY_RUNS_PER_CYCLE) -> int:
    """One verification cycle: newest runs first, up to `limit`. Returns rows
    written. Imports Supabase helpers lazily so the pure parts stay import-
    light for tests."""
    from gateway.services import supabase as sb
    from gateway.routes.ledger import _flagship_addresses  # allowlist

    if not sb.sb_enabled():
        return 0
    metas = await sb.fetch_flagship_runs(limit=200)
    existing = await sb.fetch_leg_verifications()
    hints = await sb.fetch_payto_hints()
    todo = [m for m in metas if needs_check(m, existing)][:limit]
    if not todo:
        return 0
    written = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        for meta in todo:
            try:
                rows = await verify_run(meta, _flagship_addresses(), hints, client)
            except Exception as e:      # RPC hiccup — try again next cycle
                logger.warning(f"[leg-verifier] {meta.get('run_at')}: {e}")
                continue
            if rows and await sb.upsert_leg_verifications(rows):
                written += len(rows)
                logger.info(f"[leg-verifier] {meta.get('run_at')}: "
                            f"{len(rows) - 1} leg(s) chain-verified")
    return written


async def verify_loop() -> None:
    """Background verifier — register in main.py's lifespan. First pass runs
    shortly after boot so a fresh deploy backfills without waiting a cycle."""
    await asyncio.sleep(90)
    while True:
        try:
            n = await verify_pending()
            if n:
                from gateway.routes.ledger import _invalidate_ledger_cache
                _invalidate_ledger_cache()
        except Exception as e:  # pragma: no cover
            logger.warning(f"[leg-verifier] cycle failed: {e}")
        await asyncio.sleep(VERIFY_INTERVAL_SECONDS)
