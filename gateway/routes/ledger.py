"""
routes/ledger.py — Public flagship receipt ledger.

  GET /ledger        — self-contained HTML page (styled like /radar)
  GET /ledger.json   — machine-readable run history (the HTML fetches this)

This is the public proof point for AgentPay's positioning: an autonomous agent
(the flagship analyst, agents/analyst/run.py) that prices a plan, spends real
USDC under a hard per-run cap, and leaves a verifiable on-chain receipt every
day. The ledger reads the durable payment_logs table and reconstructs the
agent's runs — free intel calls + paid verdicts — with spend-vs-cap and
block-explorer links for every paid call.

Design notes:
  * Read-only, additive, public, unauthenticated. Behind LEDGER_ENABLED (default
    on) so it can be 404'd without a redeploy, mirroring RADAR_ENABLED.
  * The flagship is identified by an allowlist of its wallet addresses — its
    Base payer (paid pre_trade_check verdicts settle here, eip155:8453) and its
    Stellar free-tier identity (free intel calls log here at $0). Both legs carry
    the agent's address; the abandoned Stellar challenge legs (NULL address) are
    naturally excluded.
  * Only state='payment_done' rows count — a completed call. Free = $0, paid > $0.
  * Runs are reconstructed by time-clustering: a gap larger than _RUN_GAP_SECONDS
    starts a new run. The flagship runs once daily in a ~40s burst, so a 30-min
    gap cleanly separates runs without ever splitting one.
  * group_runs() is a PURE function (no I/O) so it's unit-tested directly.
"""

import logging
import time as _time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import hmac
import re

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from gateway._limiter import limiter
from gateway.config import settings
from gateway.services.leg_verifier import run_key as _run_key
from gateway.services.supabase import (
    fetch_flagship_runs,
    fetch_leg_verifications,
    insert_flagship_run,
    sb_enabled,
    sb_headers,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# A new run starts when the gap between consecutive completed calls exceeds this.
# The flagship's whole run is a sub-minute burst once per day, so 30 min is a
# wide, safe separator that never splits a single run.
_RUN_GAP_SECONDS = 30 * 60

# Built-in flagship wallet allowlist. Overridable via LEDGER_FLAGSHIP_ADDRESSES
# (comma-separated) without a code change. Matched case-insensitively.
_DEFAULT_FLAGSHIP_ADDRESSES = [
    "0xe1601C10B8d4DbF71E0c592B779520380174bc3A",            # Base payer (verdicts)
    "GAACF3K43CEWDO2BMOGT3K3GSETBINQFXZ3EQFJUWFLYNTCRHRAA3KVD",  # Stellar identity (free intel)
]

# What each tool call contributes to the decision — used to narrate the
# execution timeline ("step 3: checked perp funding ($0)"). Keeps the ledger
# legible as a sequence of decisions, not a list of opaque tool names.
_TOOL_PURPOSE = {
    "fear_greed_index":  "read market sentiment",
    "funding_rates":     "check perp funding",
    "market_snapshot":   "pull a price snapshot",
    "crypto_news":       "scan catalysts & news",
    "gas_tracker":       "gauge ETH network demand",
    "defi_tvl":          "survey the DeFi landscape",
    "open_interest":     "check derivatives positioning",
    "orderbook_depth":   "measure order-book liquidity",
    "token_market_data": "pull token market data",
    "token_security":    "screen contract risk",
    "whale_activity":    "track large on-chain flows",
    "yield_scanner":     "scan yield opportunities",
    "token_price":       "fetch a token price",
    "wallet_balance":    "check a wallet balance",
    "dune_query":        "run an on-chain query",
    "session_create":    "open a spending session",
    "pre_trade_check":   "buy a trade-safety verdict",
    "verified_route":    "vet the marketplace before paying",
}

# Off-gateway x402 endpoints the agent pays directly (not registry tools, so
# they never carry a clean tool name). Matched by substring on the call's URL.
_URL_PURPOSE = [
    ("/dex/search", ("cmc_dex_search", "search CMC for the DEX token")),
    ("/dex/pairs",  ("cmc_dex_pairs", "buy CMC DEX pair liquidity")),
    ("coinmarketcap.com", ("cmc_x402", "pull vetted CMC market data")),
]


def _purpose(tool: str | None) -> str:
    return _TOOL_PURPOSE.get(tool or "", f"call {tool}")


def _label_external(tool: str | None) -> tuple[str, str]:
    """Map an off-gateway x402 URL (e.g. a CMC endpoint) to a (short_name,
    purpose) pair so it reads cleanly in the timeline instead of as a raw URL."""
    t = tool or ""
    for needle, label in _URL_PURPOSE:
        if needle in t:
            return label
    short = t.rsplit("/", 1)[-1] or t
    return (short, f"call {short}")


def _build_timeline(free_calls: list[dict], paid_calls: list[dict],
                    cap: Decimal) -> list[dict]:
    """Merge a run's free + paid calls into one execution-ordered sequence with
    the budget drawing down at each step — the 'how the plan ran, step by step'
    view. PURE."""
    merged = []
    for c in free_calls:
        merged.append({**c, "kind": "free", "amount_usdc": "0.00"})
    for c in paid_calls:
        merged.append({**c, "kind": "paid"})
    merged.sort(key=lambda c: c.get("at") or "")

    steps = []
    spent = Decimal("0")
    for i, c in enumerate(merged, start=1):
        amt = _dec(c.get("amount_usdc"))
        spent += amt
        step = {
            "step":               i,
            "tool":               c.get("tool"),
            "purpose":            _purpose(c.get("tool")),
            "kind":               c["kind"],
            "cost_usdc":          f"{amt:.2f}",
            "running_spent_usdc": f"{spent:.2f}",
            "remaining_usdc":     f"{(cap - spent):.2f}",
            "at":                 c.get("at"),
        }
        if c["kind"] == "paid":
            step["network"] = c.get("network")
            step["tx_hash"] = c.get("tx_hash")
            step["explorer_url"] = c.get("explorer_url")
        steps.append(step)
    return steps


# AGE-75: a Stellar public key (G + 55 base32) or an EVM address (0x + 40 hex).
# Config-sourced addresses are interpolated into a PostgREST `or=(ilike…)`
# clause, so a stray `%`, `,`, or `(` in LEDGER_FLAGSHIP_ADDRESSES would broaden
# the filter (and could surface unrelated wallets' rows on the public ledger).
# Validate shape and drop anything that doesn't match before building the query.
_ADDR_RE = re.compile(r"^(G[A-Z2-7]{55}|0x[0-9a-fA-F]{40})$")


def _flagship_addresses() -> list[str]:
    raw = (settings.LEDGER_FLAGSHIP_ADDRESSES or "").strip()
    if raw:
        addrs = []
        for a in (x.strip() for x in raw.split(",")):
            if not a:
                continue
            if _ADDR_RE.match(a):
                addrs.append(a)
            else:
                logger.warning(
                    f"[ledger] dropping malformed LEDGER_FLAGSHIP_ADDRESSES entry: {a!r}"
                )
        if addrs:
            return addrs
        logger.error("[ledger] LEDGER_FLAGSHIP_ADDRESSES had no valid entries — "
                     "using built-in defaults")
    return list(_DEFAULT_FLAGSHIP_ADDRESSES)


def _norm_network(network: str | None) -> str:
    """Normalize the stored network label to a short chain name."""
    n = (network or "").lower()
    if n.startswith("eip155:8453") or n == "base-mainnet" or n == "base":
        return "base"
    if "84532" in n or n == "base-sepolia":
        return "base-sepolia"
    if n == "stellar-testnet":
        return "stellar-testnet"
    if n.startswith("stellar"):
        return "stellar"
    return n or "unknown"


def _explorer_url(network: str, tx_hash: str | None) -> str | None:
    """Block-explorer link for a tx on a given (normalized) chain."""
    if not tx_hash:
        return None
    return {
        "base":           f"https://basescan.org/tx/{tx_hash}",
        "base-sepolia":   f"https://sepolia.basescan.org/tx/{tx_hash}",
        "stellar":        f"https://stellar.expert/explorer/public/tx/{tx_hash}",
        "stellar-testnet": f"https://stellar.expert/explorer/testnet/tx/{tx_hash}",
    }.get(network)


def _dec(amount: str | None) -> Decimal:
    try:
        return Decimal(str(amount or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _money_to_dec(value) -> Decimal:
    """Parse an SDK receipt money string like '$0.010' (or a number) to Decimal."""
    if value is None:
        return Decimal("0")
    s = str(value).strip().lstrip("$").replace(",", "")
    try:
        return Decimal(s or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_ts(value: str) -> datetime | None:
    """Parse a Postgres ISO timestamp robustly.

    Python 3.10's datetime.fromisoformat only accepts 0/3/6 fractional digits,
    but Postgres emits a variable count (e.g. '...35.61428+00:00', 5 digits).
    Normalize the fractional part to exactly 6 digits and the offset to ±HH:MM
    before parsing, so no row silently fails (a dropped parse breaks run
    clustering by hiding a gap)."""
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:?\d{2})?$",
        s,
    )
    if m:
        base, frac, tz = m.group(1), m.group(2), m.group(3) or "+00:00"
        base = base.replace(" ", "T")
        frac = "." + (frac[:6].ljust(6, "0")) if frac else ""
        if len(tz) == 5 and ":" not in tz:  # ±HHMM → ±HH:MM
            tz = tz[:3] + ":" + tz[3:]
        s = base + frac + tz
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def group_runs(rows: list[dict], run_cap: str = "0.25") -> dict:
    """Reconstruct flagship runs from completed payment_logs rows.

    PURE — no I/O. `rows` may be in any order; only state='payment_done' rows
    are considered. Returns a dict with `totals` and a `runs` list (newest run
    first), each run carrying its free/paid calls, spend, and the cap it ran
    under.
    """
    cap = _dec(run_cap)
    completed = [r for r in rows if (r.get("state") == "payment_done")]
    # Sort ascending by timestamp for clustering.
    completed.sort(key=lambda r: r.get("created_at") or "")

    runs: list[dict] = []
    current: dict | None = None
    prev_dt: datetime | None = None

    for r in completed:
        dt = _parse_ts(r.get("created_at") or "")
        gap = None
        if prev_dt is not None and dt is not None:
            gap = (dt - prev_dt).total_seconds()
        if current is None or (gap is not None and gap > _RUN_GAP_SECONDS):
            current = {
                "started": r.get("created_at"),
                "ended": r.get("created_at"),
                "free_calls": [],
                "paid_calls": [],
                "spent_usdc": Decimal("0"),
                "cap_usdc": cap,
            }
            runs.append(current)
        current["ended"] = r.get("created_at")

        prev_dt = dt if dt is not None else prev_dt

        net = _norm_network(r.get("network"))
        amount = _dec(r.get("amount_usdc"))
        tool = r.get("tool_name")
        if amount > 0:
            tx = r.get("tx_hash")
            current["paid_calls"].append({
                "tool": tool,
                "amount_usdc": f"{amount:.2f}",
                "network": net,
                "tx_hash": tx,
                "explorer_url": _explorer_url(net, tx),
                "at": r.get("created_at"),
            })
            current["spent_usdc"] += amount
        else:
            current["free_calls"].append({"tool": tool, "network": net, "at": r.get("created_at")})

    # Finalize: format decimals, compute per-run counts, newest-first.
    out_runs = []
    total_spent = Decimal("0")
    total_paid = total_free = 0
    for run in runs:
        spent = run["spent_usdc"]
        total_spent += spent
        total_paid += len(run["paid_calls"])
        total_free += len(run["free_calls"])
        # Running budget: walk the paid calls in order, showing the cap drawing
        # down with each settlement — the on-chain view of "the cap is the law".
        run_cap_dec = run["cap_usdc"]
        cumulative = Decimal("0")
        for p in run["paid_calls"]:
            cumulative += _dec(p["amount_usdc"])
            p["spent_after_usdc"] = f"{cumulative:.2f}"
            p["remaining_after_usdc"] = f"{(run_cap_dec - cumulative):.2f}"
        out_runs.append({
            "started": run["started"],
            "ended": run["ended"],
            "free_count": len(run["free_calls"]),
            "paid_count": len(run["paid_calls"]),
            "free_calls": run["free_calls"],
            "paid_calls": run["paid_calls"],
            "timeline": _build_timeline(run["free_calls"], run["paid_calls"], run_cap_dec),
            "spent_usdc": f"{spent:.2f}",
            "cap_usdc": f"{run_cap_dec:.2f}",
            "remaining_usdc": f"{(run_cap_dec - spent):.2f}",
            "under_cap": spent <= run_cap_dec,
        })
    out_runs.reverse()  # newest run first

    return {
        "totals": {
            "runs": len(out_runs),
            "paid_calls": total_paid,
            "free_calls": total_free,
            "spent_usdc": f"{total_spent:.2f}",
            "first_run": runs[0]["started"] if runs else None,
            "last_run": runs[-1]["ended"] if runs else None,
        },
        "runs": out_runs,
    }


def attach_reasoning(runs: list[dict], metas: list[dict]) -> int:
    """Attach flagship run metadata (plan, regime, verdicts, receipt, …) to the
    grouped run whose [started, ended] window (±5 min) contains the meta's
    run_at. PURE — mutates `runs` in place. Both lists are newest-first. Returns
    the number of runs enriched. Runs with no matching meta keep their on-chain
    view only (graceful degradation before the table is populated)."""
    used: set[int] = set()
    enriched = 0
    for run in runs:
        start = _parse_ts(run.get("started") or "")
        end = _parse_ts(run.get("ended") or "")
        if not (start and end):
            continue
        lo, hi = start.timestamp() - 300, end.timestamp() + 300
        for i, m in enumerate(metas):
            if i in used:
                continue
            mt = _parse_ts(m.get("run_at") or "")
            if mt and lo <= mt.timestamp() <= hi:
                obj = m.get("objective") or {}
                run["reasoning"] = {
                    "run_at":     m.get("run_at"),
                    "objective":  obj,
                    "kind":       obj.get("kind") or "pre_trade",
                    "goal_text":  obj.get("goal_text") or "",
                    "plan":       m.get("plan") or {},
                    "regime":     m.get("regime") or "",
                    "context":    m.get("context") or "",
                    "verdicts":   m.get("verdicts") or {},
                    "skipped":    m.get("skipped") or {},
                    "findings":   m.get("findings") or {},
                    "receipt":    m.get("receipt") or {},
                    "free_intel": m.get("free_intel") or {},
                    "note":       m.get("note") or "",
                }
                used.add(i)
                enriched += 1
                break
    return enriched


def _run_view_from_breakdown(
    breakdown: list[dict], cap: Decimal,
    verified_legs: "Counter | None" = None,
    chain_legs: "dict[int, dict] | None" = None,
) -> dict:
    """Walk an SDK receipt breakdown into the ledger's run-view fields
    (timeline, paid_calls, counts, spend). Shared by reconcile_from_receipt
    and synthesize_offgateway_runs.

    AGE-63: these views are rebuilt WHOLESALE from the agent-posted receipt,
    so their tx hashes are only as trustworthy as whoever holds
    FLAGSHIP_INGEST_SECRET. `verified_legs` is a CONSUMABLE Counter of the
    gateway-settled legs from payment_logs, keyed on `(tx_hash_lower, amount)`.
    Each paid leg is tagged `verification`:
        "onchain"        — an unconsumed payment_logs leg matches BOTH this
                           leg's tx_hash AND its amount; the match is consumed
                           so one real settlement can be counted at most once
                           (a holder of the ingest secret can't reuse a single
                           real hash across many fabricated legs to inflate
                           "verified" spend).
        "onchain_chain"  — AGE-142: not settled through the gateway, but the
                           leg-verifier found a matching USDC transfer FROM the
                           run's wallet on Base in the run window (see
                           gateway/services/leg_verifier.py; `chain_legs` is
                           {leg_index: cached row} for THIS run). We observed
                           the settlement rather than performing it — hence the
                           distinct label; `verification_method` says how it
                           matched (chain:hash / chain:amount+payto / chain:amount).
        "no_settlement_found" — AGE-142: the verifier DID check this run
                           (marker row present) and found no USDC transfer for
                           this leg. The SDK books spend fail-closed the moment
                           a signed authorization is transmitted (AGE-56), so a
                           seller that rejected the call after that still shows
                           as spend in the receipt — this label says the money
                           never actually left the wallet. Not verified, not
                           attested: booked-but-unsettled.
        "agent_attested" — no matching on-chain leg and the run has NOT been
                           chain-checked yet (or has no Base wallet to check).
                           Either a legitimate off-gateway leg we haven't
                           looked for, or a fabricated entry — indistinguishable
                           from here, so NOT presented as on-chain-verified.
    NOTE: not pure — consumes from the passed Counter, which is shared across
    all runs in one ledger render so matches can't be double-spent between
    reconcile and synthesize. `verified_legs=None` → all paid legs attested."""
    vlegs = verified_legs if verified_legs is not None else Counter()
    clegs = chain_legs or {}
    run_checked = _CHAIN_MARKER in clegs
    steps: list[dict] = []
    paid_calls: list[dict] = []
    spent = Decimal("0")
    verified_spent = attested_spent = chain_spent = unsettled_spent = Decimal("0")
    free_count = paid_count = attested_count = chain_count = unsettled_count = 0

    for i, e in enumerate(breakdown, start=1):
        raw_tool = e.get("tool")
        external = bool(raw_tool) and ("://" in raw_tool or "/" in raw_tool)
        name, purpose = (_label_external(raw_tool) if external
                         else (raw_tool, _purpose(raw_tool)))
        amt = _money_to_dec(e.get("cost"))
        spent += amt
        step = {
            "step": i, "tool": name, "purpose": purpose,
            "cost_usdc": f"{amt:.2f}",
            "running_spent_usdc": f"{spent:.2f}",
            "remaining_usdc": f"{(cap - spent):.2f}",
        }
        if amt > 0:
            net = _norm_network(e.get("network"))
            tx = e.get("tx_hash") or None
            # Match on (hash, amount) AND consume, so a real hash reused across
            # legs — or with a fabricated cost — is only credited once, for the
            # exact amount that actually settled.
            key = (tx.lower(), amt) if tx else None
            verified = bool(key and vlegs.get(key, 0) > 0)
            if verified:
                vlegs[key] -= 1
            method: str | None = "gateway" if verified else None
            if verified:
                verification = "onchain"
                verified_spent += amt
            elif i in clegs and _money_to_dec(clegs[i].get("amount_usdc")) == amt:
                # AGE-142: chain evidence for this leg (cached by the verifier).
                # The cached amount must equal the receipt's — a mismatch means
                # the receipt changed under the cache; treat as unverified.
                row = clegs[i]
                verification = "onchain_chain"
                tx = tx or row.get("tx_hash") or None
                method = f"chain:{row.get('method') or 'amount'}"
                chain_spent += amt
                chain_count += 1
                verified_spent += amt
            elif run_checked:
                verification = "no_settlement_found"
                unsettled_spent += amt
                unsettled_count += 1
            else:
                verification = "agent_attested"
                attested_spent += amt
                attested_count += 1
            # Only surface an explorer link for legs we could actually verify —
            # a link on an unverified hash reads as "we checked this" when we
            # didn't.
            explorer = (_explorer_url(net, tx)
                        if verification in ("onchain", "onchain_chain") else None)
            step.update({"kind": "paid", "network": net,
                         "tx_hash": tx, "explorer_url": explorer,
                         "verification": verification,
                         "verification_method": method})
            paid_calls.append({
                "tool": name, "amount_usdc": f"{amt:.2f}", "network": net,
                "tx_hash": tx, "explorer_url": explorer,
                "verification": verification,
                "verification_method": method,
                "spent_after_usdc": f"{spent:.2f}",
                "remaining_after_usdc": f"{(cap - spent):.2f}",
            })
            paid_count += 1
        else:
            step["kind"] = "free"
            free_count += 1
        steps.append(step)

    return {
        "timeline": steps, "paid_calls": paid_calls,
        "paid_count": paid_count, "free_count": free_count,
        "spent_usdc": f"{spent:.2f}",
        "remaining_usdc": f"{(cap - spent):.2f}",
        "under_cap": spent <= cap,
        # AGE-63: verification breakdown for this receipt-derived view.
        # verified = gateway-settled + chain-verified (AGE-142); the chain
        # share is broken out so a reader can tell observed from performed.
        "verified_spent_usdc": f"{verified_spent:.2f}",
        "chain_verified_spent_usdc": f"{chain_spent:.2f}",
        "chain_verified_paid_count": chain_count,
        "attested_spent_usdc": f"{attested_spent:.2f}",
        "attested_paid_count": attested_count,
        "unsettled_spent_usdc": f"{unsettled_spent:.2f}",
        "unsettled_paid_count": unsettled_count,
        "has_attested_spend": attested_count > 0,
        "has_chain_verified_spend": chain_count > 0,
        "has_unsettled_spend": unsettled_count > 0,
    }


_CHAIN_MARKER = -1   # leg_index of the verifier's "run checked" marker row


def _chain_legs_for(chain_index: "dict[tuple[str, int], dict] | None",
                    run_at) -> dict[int, dict]:
    """Slice the global verification cache down to one run: {leg_index: row}.
    The marker row (leg_index -1) is kept so the view can tell "checked, no
    transfer found" from "never checked"."""
    if not chain_index or not run_at:
        return {}
    k = _run_key(run_at)
    return {idx: row for (rk, idx), row in chain_index.items() if rk == k}


def synthesize_offgateway_runs(runs: list[dict], metas: list[dict],
                               run_cap: str = "0.25",
                               verified_legs: "Counter | None" = None,
                               chain_index: "dict[tuple[str, int], dict] | None" = None) -> int:
    """Surface runs that never touched payment_logs at all. PURE — mutates
    `runs` in place (keeps newest-first order); returns the count added.

    The prober's probe_sweep runs pay sellers DIRECTLY (agent→seller x402),
    so group_runs — which clusters payment_logs — has nothing to cluster and
    attach_reasoning finds no window to attach their metadata to (AGE-10,
    found live 2026-07-10: 0 probe runs on /ledger despite stored metas).
    For each probe_sweep meta whose run_at falls inside no existing run
    window, synthesize a run straight from its SDK receipt breakdown — the
    same authoritative source reconcile_from_receipt trusts for the strategy
    goal's off-gateway CMC legs.
    """
    windows = []
    for run in runs:
        s, e = _parse_ts(run.get("started") or ""), _parse_ts(run.get("ended") or "")
        if s and e:
            windows.append((s.timestamp() - 300, e.timestamp() + 300))

    added = 0
    for m in metas:
        obj = m.get("objective") or {}
        if (obj.get("kind") or "") != "probe_sweep":
            continue
        mt = _parse_ts(m.get("run_at") or "")
        if mt is None or any(lo <= mt.timestamp() <= hi for lo, hi in windows):
            continue    # unparseable, or already covered by a clustered run
        receipt = m.get("receipt") or {}
        breakdown = receipt.get("breakdown")
        if not isinstance(breakdown, list):
            breakdown = []
        cap = _dec(obj.get("cap_usdc") or m.get("max_spend") or run_cap)
        view = _run_view_from_breakdown(breakdown, cap, verified_legs,
                                        _chain_legs_for(chain_index, m.get("run_at")))
        runs.append({
            "started": m.get("run_at"),
            "ended": m.get("run_at"),
            "free_calls": [],
            "cap_usdc": f"{cap:.2f}",
            **view,
            "synthesized_offgateway": True,
            "reasoning": {
                "objective":  obj,
                "kind":       "probe_sweep",
                "goal_text":  obj.get("goal_text") or "",
                "plan":       m.get("plan") or {},
                "regime":     m.get("regime") or "",
                "context":    m.get("context") or "",
                "verdicts":   m.get("verdicts") or {},
                "skipped":    m.get("skipped") or {},
                "findings":   m.get("findings") or {},
                "receipt":    receipt,
                "free_intel": m.get("free_intel") or {},
                "note":       m.get("note") or "",
            },
        })
        added += 1

    if added:
        runs.sort(key=lambda r: _parse_ts(r.get("started") or "")
                  or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return added


def _run_keeps_payment_logs_view(run: dict) -> bool:
    """True if this run will NOT be rebuilt by reconcile_from_receipt — the
    single predicate shared by reconcile and preconsume_rendered_legs (F4)
    so the two can never disagree about which runs are receipt-derived."""
    rz = run.get("reasoning") or {}
    if (rz.get("kind") or "") != "strategy":
        return True
    breakdown = (rz.get("receipt") or {}).get("breakdown")
    return not (isinstance(breakdown, list) and breakdown)


def preconsume_rendered_legs(runs: list[dict],
                             verified_legs: "Counter | None") -> int:
    """F4 (2026-07-20): consume from `verified_legs` every settlement already
    rendered by a run that KEEPS its payment_logs view.

    The Counter is seeded from *every* flagship payment_logs leg, but only
    receipt-derived views (reconcile/synthesize) consumed from it — legs
    shown in ordinary runs never did. A holder of FLAGSHIP_INGEST_SECRET
    could therefore post a fabricated receipt whose leg reuses a real run's
    public tx_hash + amount: it rendered verification="onchain" with an
    explorer link, and _recompute_totals counted that one settlement as
    verified spend twice. Consuming the ordinary runs' legs FIRST means a
    settlement already displayed on the ledger can't be re-credited to an
    agent-posted receipt. PURE apart from mutating the Counter; returns the
    number of legs consumed."""
    if not verified_legs:
        return 0
    # tx → candidate keys. Amounts in run paid_calls are display-formatted
    # ("%.2f" — a $0.001 leg reads "0.00"), so match on the hash and consume
    # the settlement's actual (tx, amount) key. payment_logs tx hashes are
    # unique per settlement, so a tx maps to one real key (Counter
    # multiplicity covers pathological duplicates).
    by_tx: dict[str, list] = {}
    for key in verified_legs:
        if verified_legs[key] > 0:
            by_tx.setdefault(key[0], []).append(key)
    consumed = 0
    for run in runs:
        if not _run_keeps_payment_logs_view(run):
            continue
        for p in run.get("paid_calls") or []:
            tx = str(p.get("tx_hash") or "").lower()
            for key in by_tx.get(tx, ()):
                if verified_legs.get(key, 0) > 0:
                    verified_legs[key] -= 1
                    consumed += 1
                    break
    return consumed


def reconcile_from_receipt(runs: list[dict],
                           verified_legs: "Counter | None" = None,
                           chain_index: "dict[tuple[str, int], dict] | None" = None) -> int:
    """Rebuild a strategy run's timeline from its SDK receipt breakdown.

    PURE — mutates `runs` in place; returns the count reconciled. The
    payment_logs-derived timeline only sees calls that settle THROUGH the
    gateway, so a strategy run's direct CMC x402 legs (paid agent→CMC, off
    gateway) are invisible to it — the timeline then under-reports what the
    run's own receipt records. The SDK's `spending_summary().breakdown` is the
    authoritative per-call ledger (every call, cost, tx, chain — gateway AND
    off-gateway), in execution order. Rebuilding from it makes the timeline,
    paid-call list, and spend totals match the receipt by construction.

    Scoped to strategy runs (where the off-gateway legs occur); other runs keep
    their on-chain payment_logs view untouched.
    """
    reconciled = 0
    for run in runs:
        # F4: predicate shared with preconsume_rendered_legs — keep in sync.
        if _run_keeps_payment_logs_view(run):
            continue
        breakdown = ((run.get("reasoning") or {}).get("receipt") or {}).get("breakdown")

        cap = _dec(run.get("cap_usdc"))
        run_at = (run.get("reasoning") or {}).get("run_at") or run.get("started")
        run.update(_run_view_from_breakdown(breakdown, cap, verified_legs,
                                            _chain_legs_for(chain_index, run_at)))
        run["reconciled_from_receipt"] = True
        reconciled += 1
    return reconciled


def _recompute_totals(data: dict) -> None:
    """Recompute top-level totals after reconciliation so /ledger.json's headline
    numbers match the (possibly reconciled) per-run spend. PURE; mutates `data`.

    AGE-63: also splits the headline spend into on-chain-verified vs
    agent-attested so a consumer can't read the total as all-verified. Runs
    with no receipt-derived view are fully on-chain (from payment_logs), so
    their spend counts as verified."""
    runs = data.get("runs") or []
    spent = sum((_dec(r.get("spent_usdc")) for r in runs), Decimal("0"))
    attested = sum(
        (_dec(r.get("attested_spent_usdc")) for r in runs
         if r.get("attested_spent_usdc") is not None),
        Decimal("0"),
    )
    chain = sum(
        (_dec(r.get("chain_verified_spent_usdc")) for r in runs
         if r.get("chain_verified_spent_usdc") is not None),
        Decimal("0"),
    )
    unsettled = sum(
        (_dec(r.get("unsettled_spent_usdc")) for r in runs
         if r.get("unsettled_spent_usdc") is not None),
        Decimal("0"),
    )
    verified = spent - attested - unsettled
    settled = spent - unsettled
    data["totals"] = {
        **data.get("totals", {}),
        "runs": len(runs),
        "paid_calls": sum(int(r.get("paid_count") or 0) for r in runs),
        "free_calls": sum(int(r.get("free_count") or 0) for r in runs),
        "spent_usdc": f"{spent:.2f}",
        # AGE-142: verified = performed (gateway) + observed (chain); both
        # broken out so nobody reads "verified" as "all settled by us".
        "verified_spent_usdc": f"{verified:.2f}",
        "gateway_verified_spent_usdc": f"{(verified - chain):.2f}",
        "chain_verified_spent_usdc": f"{chain:.2f}",
        "attested_spent_usdc": f"{attested:.2f}",
        "attested_paid_calls": sum(int(r.get("attested_paid_count") or 0) for r in runs),
        # AGE-142: booked fail-closed but no transfer ever left the wallet
        # (checked on Base). Not spend; the receipt over-counts by this sum.
        "unsettled_spent_usdc": f"{unsettled:.2f}",
        "unsettled_paid_calls": sum(int(r.get("unsettled_paid_count") or 0) for r in runs),
        "settled_spent_usdc": f"{settled:.2f}",
        "verified_share": (f"{(verified / spent):.3f}" if spent > 0 else None),
        "verified_share_of_settled": (f"{(verified / settled):.3f}" if settled > 0 else None),
    }


async def _fetch_flagship_rows() -> list[dict]:
    """Read completed payment_logs rows for the flagship's wallet allowlist."""
    if not sb_enabled():
        return []
    addrs = _flagship_addresses()
    # Case-insensitive OR over the allowlist; PostgREST `or=(...)` syntax.
    or_clause = "(" + ",".join(f"agent_address.ilike.{a}" for a in addrs) + ")"
    # AGE-62: order DESCending so the 2000-row cap keeps the NEWEST runs, not the
    # oldest. With `created_at.asc + limit`, once the flagship crossed 2000 rows
    # (~2 months at 30/day) the cap returned the oldest 2000 and new runs silently
    # stopped appearing on /ledger. We re-sort ascending in Python below so
    # group_runs (which clusters in chronological order) is unaffected.
    params = {
        "select": "created_at,tool_name,network,amount_usdc,state,tx_hash,agent_address",
        "state": "eq.payment_done",
        "or": or_clause,
        "order": "created_at.desc",
        "limit": "2000",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/payment_logs",
                headers={**sb_headers(), "Accept": "application/json"},
                params=params,
            )
        if resp.status_code != 200:
            logger.error(f"ledger fetch error: HTTP {resp.status_code} {resp.text[:200]}")
            return []
        rows = resp.json()
        # Newest-2000 came back newest-first; hand downstream the chronological
        # order it expects.
        rows.sort(key=lambda r: r.get("created_at") or "")
        return rows
    except Exception as e:
        logger.error(f"ledger fetch failure: {e}")
        return []


# AGE-72: /ledger.json is public, unauthenticated, and runs 2 Supabase queries
# + a full Python regroup on every hit — while the underlying data changes at
# most once a day. Cache the built payload in-process for a short window so a
# scrape (or the HTML page's per-load fetch) can't multiply Supabase load. The
# cache is invalidated on a successful run ingest so a new run still appears
# promptly.
_LEDGER_JSON_TTL = 60.0
_ledger_json_cache: dict = {"built_at": 0.0, "payload": None}


def _invalidate_ledger_cache() -> None:
    _ledger_json_cache["payload"] = None
    _ledger_json_cache["built_at"] = 0.0


@router.get("/ledger.json", response_class=JSONResponse)
@limiter.limit("60/minute")
async def ledger_json(request: Request):
    """Machine-readable flagship run history."""
    if not settings.LEDGER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    cached = _ledger_json_cache["payload"]
    if cached is not None and (_time.monotonic() - _ledger_json_cache["built_at"]) < _LEDGER_JSON_TTL:
        return JSONResponse(
            content=cached,
            headers={"Cache-Control": f"public, max-age={int(_LEDGER_JSON_TTL)}"},
        )
    rows = await _fetch_flagship_rows()
    data = group_runs(rows, run_cap=settings.LEDGER_RUN_CAP_USDC)
    metas = await fetch_flagship_runs()
    data["runs_with_reasoning"] = attach_reasoning(data["runs"], metas)
    # AGE-63: the authoritative, CONSUMABLE multiset of legs AgentPay actually
    # settled through the gateway — keyed on (tx_hash_lower, amount). Receipt-
    # derived views (reconcile/synthesize) match each paid leg against this and
    # consume the match, so agent-posted hashes are only shown as on-chain-
    # verified when a real settlement of that exact amount backs them, and a
    # single real settlement can't be reused across legs to inflate "verified"
    # spend. A holder of FLAGSHIP_INGEST_SECRET can no longer publish a
    # fabricated (or hash-reused) leg as a "verifiable on-chain receipt".
    verified_legs = Counter(
        (str(r["tx_hash"]).lower(), _money_to_dec(r.get("amount_usdc")))
        for r in rows if r.get("tx_hash")
    )
    # F4 (2026-07-20): settlements already rendered by ordinary
    # (non-reconciled) payment_logs runs are consumed FIRST, so a fabricated
    # receipt reusing a real run's public tx_hash can't get that settlement
    # credited as "onchain" verified spend a second time.
    preconsume_rendered_legs(data["runs"], verified_legs)
    # Reconcile off-gateway (e.g. direct CMC x402) spend into strategy-run
    # timelines from the authoritative SDK receipt, and synthesize runs that
    # never touched payment_logs at all (the prober's probe_sweeps pay sellers
    # directly), then refresh headline totals. Same Counter flows through both
    # so a match consumed by one can't be re-counted by the other.
    # AGE-142: chain-verification cache for receipt-derived legs (batch-
    # written by gateway/services/leg_verifier.py; {} until the migration and
    # first cycle land — then those legs simply stay agent_attested).
    chain_index = await fetch_leg_verifications()
    data["runs_reconciled"] = reconcile_from_receipt(data["runs"], verified_legs,
                                                     chain_index)
    data["runs_synthesized"] = synthesize_offgateway_runs(
        data["runs"], metas, run_cap=settings.LEDGER_RUN_CAP_USDC,
        verified_legs=verified_legs, chain_index=chain_index)
    if data["runs_reconciled"] or data["runs_synthesized"]:
        _recompute_totals(data)
    addrs = _flagship_addresses()
    data["agent"] = "AgentPay flagship analyst"
    data["description"] = (
        "An autonomous market analyst running on AgentPay's own rails as a real "
        "customer: it prices each run via /v1/plan/estimate, spends real USDC under "
        "a hard per-run cap, and leaves a verifiable on-chain receipt for every "
        "paid call."
    )
    data["wallets"] = {
        "base": next((a for a in addrs if a.startswith("0x")), None),
        "stellar": next((a for a in addrs if not a.startswith("0x")), None),
    }
    data["run_cap_usdc"] = f"{_dec(settings.LEDGER_RUN_CAP_USDC):.2f}"
    data["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
    # AGE-72: publish to the short-lived cache and let clients/CDN cache it too.
    _ledger_json_cache["payload"] = data
    _ledger_json_cache["built_at"] = _time.monotonic()
    return JSONResponse(
        content=data,
        headers={"Cache-Control": f"public, max-age={int(_LEDGER_JSON_TTL)}"},
    )


@router.post("/v1/flagship/run")
@limiter.limit("10/minute")   # follow-up low 2026-07-20: the 401 path let the
                              # ingest secret be brute-forced at line rate
async def flagship_ingest(request: Request,
                          x_flagship_secret: str | None = Header(default=None)):
    """Ingest a flagship run summary (plan, regime, verdicts, receipt, note).

    Secret-gated (X-Flagship-Secret must match FLAGSHIP_INGEST_SECRET). The
    gateway holds the Supabase creds and does the write so the agent stays a
    credential-free HTTP customer. 404 when the secret is unset (feature off).
    """
    secret = settings.FLAGSHIP_INGEST_SECRET
    if not secret:
        raise HTTPException(status_code=404, detail="Not found")
    # Compare bytes inside try/except so a non-latin-1 header is a clean 401,
    # not a TypeError 500 (also closes the AGE-75 roll-up item for this route).
    try:
        authorized = bool(x_flagship_secret) and hmac.compare_digest(
            x_flagship_secret.encode(), secret.encode()
        )
    except Exception:
        authorized = False
    if not authorized:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    # AGE-75: run_at is the row's key AND the idempotency key. A missing or
    # unparseable timestamp otherwise fails silently on the 202 path (and, with
    # AGE-63, would skip the existence check). Reject at the door with a 400.
    run_at = payload.get("run_at_iso") or payload.get("run_at")
    if not run_at or _parse_ts(str(run_at)) is None:
        raise HTTPException(
            status_code=400,
            detail="missing or unparseable run_at / run_at_iso (ISO-8601 required)",
        )
    stored = await insert_flagship_run(payload)
    if stored:
        # AGE-72: a new run landed — drop the cached /ledger.json so it shows up
        # immediately instead of waiting out the TTL.
        _invalidate_ledger_cache()
    # 200 when persisted; 202 when accepted-but-not-stored (e.g. table not yet
    # created) so the agent sees a 2xx and never fails its run over the ledger.
    # AGE-46: the 202 path was a silent black hole — the agent logged "ingest
    # ok" while nothing landed in flagship_runs. Log it loudly here (insert_
    # flagship_run already logs the HTTP cause) and say so in the body.
    if not stored:
        logger.warning(
            f"[FLAGSHIP] ingest ACCEPTED BUT NOT STORED "
            f"(run_at={payload.get('run_at_iso') or payload.get('run_at')!r}) — "
            f"reasoning will be missing on /ledger; check Supabase/flagship_runs"
        )
    return JSONResponse(
        {"stored": stored,
         **({} if stored else
            {"warning": "accepted but not stored — reasoning will not appear on /ledger"})},
        status_code=200 if stored else 202,
    )


@router.get("/ledger", response_class=Response)
async def ledger_page():
    """Public flagship receipt ledger — self-contained HTML."""
    if not settings.LEDGER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=_LEDGER_HTML, media_type="text/html")


# ── Self-contained HTML (fetches /ledger.json client-side, like /radar) ──────────
_LEDGER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentPay — How an Agent Decides What to Spend</title>
<meta name="description" content="The public receipt ledger: autonomous agents running on AgentPay daily under hard budget caps, with a verifiable on-chain receipt for every paid call. Plans, spend, and reasoning — live.">
<meta property="og:title" content="AgentPay Receipt Ledger — how an agent decides what to spend">
<meta property="og:description" content="Autonomous agents spending real USDC under hard caps, leaving on-chain receipts. Live and public.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://agentpay.tools/ledger">
<meta property="og:image" content="https://agentpay.tools/og.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://agentpay.tools/og.png">
<link rel="canonical" href="https://agentpay.tools/ledger">
<style>
  :root{--bg:#0b0e11;--card:#13181d;--line:#222a31;--fg:#e7edf3;--mut:#8a97a6;
        --ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--ac:#c3f53c;--base:#4f7cff;--stellar:#f5c542}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:880px;margin:0 auto;padding:28px 18px 80px}
  h1{font-size:22px;margin:0 0 6px}
  .lede{color:#c4d0dc;font-size:13.5px;margin:0 0 14px}
  .lede b{color:var(--fg)}
  .howto{background:#10151a;border:1px solid var(--line);border-radius:10px;
         padding:10px 14px;font-size:12px;color:var(--mut);margin:0 0 14px}
  .howto b{color:var(--ac)}
  .howto .arw{color:#46506a;margin:0 5px}
  .sub{color:var(--mut);font-size:12.5px;margin:0 0 20px}
  .sub code{background:#1a2128;border-radius:4px;padding:1px 5px;font-size:12px}
  .kpis{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 22px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;
       padding:12px 16px;flex:1;min-width:120px}
  .kpi .n{font-size:24px;font-weight:650;letter-spacing:-.5px}
  .kpi .l{color:var(--mut);font-size:12px;margin-top:2px}
  .kpi .n.ac{color:var(--ac)}
  .run{background:var(--card);border:1px solid var(--line);border-radius:12px;
       padding:15px 18px;margin-bottom:14px}
  .run h2{font-size:14px;margin:0 0 2px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .when{color:var(--mut);font-weight:400;font-size:12.5px}
  .pill{font-size:11px;border-radius:20px;padding:2px 9px;white-space:nowrap}
  .pill.cap{background:rgba(74,222,128,.12);color:var(--ok);border:1px solid #1f4a2f}
  .pill.over{background:rgba(251,191,36,.12);color:var(--warn);border:1px solid #4a3f1f}
  .goal{font-size:13px;color:var(--fg);background:rgba(195,245,60,.05);
        border:1px solid #2c3a18;border-radius:8px;padding:8px 12px;margin:9px 0 4px}
  .goal .lbl{color:var(--ac);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-right:7px;font-weight:700}
  .ctx{color:var(--mut);font-size:12px;margin:6px 0 2px}
  .dstep{margin:13px 0 2px}
  .dhead{font-size:12.5px;color:var(--fg);font-weight:600;margin-bottom:7px;display:flex;align-items:center;gap:8px}
  .dnum{display:inline-flex;width:19px;height:19px;border-radius:50%;background:#1c2530;
        color:var(--ac);font-size:11px;align-items:center;justify-content:center;font-weight:700;flex:none}
  .pexpl{font-size:12.5px;color:var(--mut)}
  .pexpl b{color:var(--fg);font-weight:600}
  .pexpl code{background:#1a2128;border-radius:4px;padding:1px 5px;font-size:11.5px;color:#9fb0c0}
  .tl{list-style:none;margin:0;padding:0}
  .tl li{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #1a2128;font-size:12.5px}
  .tl li:last-child{border-bottom:none}
  .tl .tn{flex:none;width:18px;color:#5f6b79;font-size:11px;text-align:right}
  .tlmain{flex:1;display:flex;flex-direction:column;line-height:1.25;min-width:0}
  .tpurpose{color:var(--fg)}
  .ttool{color:#5f6b79;font-size:10.5px}
  .tcost{flex:none;font-size:10.5px;border-radius:5px;padding:1px 7px;min-width:42px;text-align:center}
  .tcost.free{background:#1a2128;color:#8a97a6}
  .tcost.paid{background:rgba(79,124,255,.14);color:var(--base)}
  .tbud{flex:none;color:var(--mut);font-size:11.5px;min-width:64px;text-align:right;font-variant-numeric:tabular-nums}
  .tlink{flex:none;font-size:11px}
  .tatt{flex:none;font-size:9.5px;border-radius:5px;padding:1px 6px;background:#241f14;color:#d8a24a;border:1px solid #3a3020;letter-spacing:.02em;cursor:help}
  .tchain{flex:none;font-size:9.5px;border-radius:5px;padding:1px 6px;background:#12241a;color:#5fc48a;border:1px solid #1f3a2a;letter-spacing:.02em;cursor:help}
  .tunset{flex:none;font-size:9.5px;border-radius:5px;padding:1px 6px;background:#1c1f24;color:#8a94a3;border:1px solid #2a2f38;letter-spacing:.02em;cursor:help}
  .attnote{font-size:11px;color:var(--mut);margin:2px 0 8px;line-height:1.5}
  .attnote .tatt{cursor:default}
  .verds{margin:2px 0}
  .vd{font-size:12.5px;margin:5px 0;color:var(--fg)}
  .verd{font-size:10px;border-radius:5px;padding:1px 7px;font-weight:700;letter-spacing:.03em}
  .verd.ok{background:rgba(74,222,128,.14);color:var(--ok)}
  .verd.caution{background:rgba(251,191,36,.14);color:var(--warn)}
  .verd.avoid{background:rgba(248,113,113,.15);color:#f87171}
  .receipt{margin-top:13px;border-top:1px solid #1a2128;padding-top:10px}
  .spendbar{height:7px;background:#1c232a;border-radius:4px;overflow:hidden;margin:0 0 6px}
  .spendbar i{display:block;height:100%;background:var(--ac)}
  .spendmeta{color:var(--mut);font-size:12px}
  .spendmeta b{color:var(--fg);font-weight:600}
  a{color:var(--ac);text-decoration:none}
  a:hover{text-decoration:underline}
  .msg{color:var(--mut);padding:30px 0;text-align:center}
  .foot{color:var(--mut);font-size:12px;margin-top:22px}
  .foot a{color:var(--mut);text-decoration:underline}
  .mut{color:var(--mut)}
  .posnote{color:#8a97a6;font-size:11.5px}
  .vhead{display:flex;align-items:center;gap:8px;font-size:13px}
  .subt{margin:7px 0 0;display:grid;gap:3px}
  .subt .row{font-size:11.5px;color:var(--mut);display:flex;gap:8px;align-items:baseline}
  .subt .tn2{min-width:120px;color:#9fb0c0}
  .lvl{font-size:9.5px;border-radius:4px;padding:0 5px;letter-spacing:.02em}
  .lvl.ok{background:rgba(74,222,128,.13);color:var(--ok)}
  .lvl.caution{background:rgba(251,191,36,.13);color:var(--warn)}
  .lvl.avoid{background:rgba(248,113,113,.14);color:var(--bad)}
  .lvl.skipped{background:#1a2128;color:#6b7886}
  .bought2{font-size:11.5px;color:#7e8a98;font-style:italic;margin-top:8px}
  .readout{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-top:5px}
  .rd{background:#10151a;border:1px solid #1a2128;border-radius:8px;padding:8px 10px}
  .rd .l{font-size:10px;color:#6b7886;text-transform:uppercase;letter-spacing:.04em}
  .rd .v{font-size:14px;color:var(--fg);margin-top:2px}
</style></head><body><div class="wrap">

<h1>AgentPay — Budgeted Data Access for Autonomous Agents</h1>
<p class="lede"><b>AgentPay gives an AI agent priced, capped, receipted access to live data tools.</b>
Below, an autonomous analyst runs on it daily — each run it asks a <b>different real question</b>,
prices the data plan <b>before</b> paying, spends only a few cents under a hard cap it cannot
exceed, and leaves a verifiable on-chain receipt. The cap governs <b>data spend</b> — not any
capital the strategy trades.</p>
<div class="howto"><b>EACH RUN</b><span class="arw">·</span>
  Ask a question <span class="arw">→</span> Price the data plan
  <span class="arw">→</span> Does it fit the cap? <span class="arw">→</span>
  Spend under the cap <span class="arw">→</span> What came back <span class="arw">→</span> Receipt</div>
<p class="sub" id="sub"></p>

<div class="kpis" id="kpis"></div>
<div id="runs"><div class="msg">Loading…</div></div>

<p class="foot">
  Each run is one decision cycle. Paid calls are real USDC settlements on Base,
  verifiable on-chain; free intel calls settle $0 on Stellar but still produce a
  receipt. Source of truth: the durable <code>payment_logs</code> +
  <code>flagship_runs</code> ledger.<br>
  <a href="/ledger.json">/ledger.json</a> · <a href="https://github.com/romudille-bit/agentpay">github.com/romudille-bit/agentpay</a>
</p>

<script>
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money = s => "$"+Number(s||0).toFixed(2);
const num = n => Number(n||0).toLocaleString();
function fmtWhen(iso){ if(!iso) return ""; const d=new Date(iso);
  return d.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",timeZoneName:"short"}); }
function shortHash(h){ return h? h.slice(0,8)+"…"+h.slice(-6) : ""; }

// ① the goal the agent set for itself this run
function goalText(rz, run){
  if(rz && rz.goal_text){
    let t = esc(rz.goal_text);
    const o = rz.objective||{};
    if(o.trade_size_usd)
      t += ` <span class="posnote">· position size $${num(o.trade_size_usd)} — capital, not the intel budget</span>`;
    return t;
  }
  const o=(rz&&rz.objective)||{};
  const syms=Array.isArray(o.symbols)&&o.symbols.length? o.symbols.join(", ") : null;
  if(syms) return `Screen ${esc(syms)} with live data, under a ${money(run.cap_usdc)} intel budget.`;
  return `Gather market data under a ${money(run.cap_usdc)} intel budget.`;
}
function humanUsd(n){ n=Number(n); if(!isFinite(n)) return "—";
  for(const [s,d] of [["T",1e12],["B",1e9],["M",1e6],["K",1e3]]) if(Math.abs(n)>=d) return "$"+(n/d).toFixed(1)+s;
  return "$"+n.toFixed(0); }
function cell(l,v){ return `<div class="rd"><div class="l">${esc(l)}</div><div class="v">${v}</div></div>`; }

// ② plan + the pre-flight price check (the AgentPay decision moment)
function planStep(rz, run){
  const plan=(rz&&rz.plan)||{};
  const steps=Array.isArray(plan.steps)? plan.steps.length : (run.timeline||[]).length;
  if(plan.total_usdc!=null){
    const fit = plan.fits_budget===false
      ? `<span class="lvl caution">trimmed to fit</span>`
      : `<span class="lvl ok">fits the ${money(run.cap_usdc)} cap ✓</span>`;
    return `<div class="dstep"><div class="dhead"><span class="dnum">1</span> Price the data plan — before paying</div>
      <div class="pexpl">Estimated all <b>${steps} calls</b> at <b>${money(plan.total_usdc)}</b> up front via <code>/v1/plan/estimate</code>, then checked it against the cap: ${fit}</div></div>`;
  }
  return `<div class="dstep"><div class="dhead"><span class="dnum">1</span> Plan the run</div>
    <div class="pexpl"><b>${steps} calls</b> planned — ${run.free_count} free + ${run.paid_count} paid, under a ${money(run.cap_usdc)} cap.</div></div>`;
}

// ③ execute the plan step by step, budget drawing down
function execStep(run){
  const items=(run.timeline||[]).map(s=>{
    const cost = s.kind==="paid"? money(s.cost_usdc) : "free";
    // AGE-63: only an on-chain-verified leg gets the explorer link; an
    // agent-attested leg (off-gateway or unverifiable) is labelled as such
    // instead of being dressed up as a checked on-chain receipt.
    let mark = "";
    if(s.kind==="paid"){
      if(s.verification==="agent_attested"){
        mark = `<span class="tatt" title="Reported by the agent's signed receipt; not settled through AgentPay's gateway and no matching USDC transfer from the agent's wallet was found on Base, so not independently verified">agent-attested</span>`;
      } else if(s.verification==="no_settlement_found"){
        mark = `<span class="tunset" title="The agent's receipt booked this spend fail-closed (a signed authorization was transmitted), but AgentPay checked the agent's wallet on Base for the run window and found no USDC transfer for this leg — the seller never settled it. Money did not leave the wallet.">no settlement found</span>`;
      } else if(s.verification==="onchain_chain"){
        const how = (s.verification_method||"chain").replace("chain:","");
        mark = `<span class="tchain" title="Not settled through AgentPay's gateway; AgentPay located the USDC transfer from the agent's wallet on Base for this leg (match: ${esc(how)})">chain-verified</span>`
             + (s.explorer_url? ` <a class="tlink" href="${esc(s.explorer_url)}" target="_blank" rel="noopener">tx ↗</a>` : "");
      } else if(s.explorer_url){
        mark = `<a class="tlink" href="${esc(s.explorer_url)}" target="_blank" rel="noopener">tx ↗</a>`;
      }
    }
    return `<li>
      <span class="tn">${s.step}</span>
      <div class="tlmain"><span class="tpurpose">${esc(s.purpose)}</span><span class="ttool">${esc(s.tool)}</span></div>
      <span class="tcost ${esc(s.kind)}">${esc(cost)}</span>
      <span class="tbud">${money(s.remaining_usdc)} left</span>
      ${mark}</li>`;
  }).join("");
  if(!items) return "";
  const note = (run.has_attested_spend || run.has_chain_verified_spend || run.has_unsettled_spend)
    ? `<div class="attnote">Legs that settled directly between the agent and the seller (off-gateway) are <span class="tchain">chain-verified</span> when AgentPay located the matching USDC transfer from the agent's wallet on Base; <span class="tunset">no settlement found</span> when the wallet was checked and no transfer exists (the receipt booked the spend fail-closed, the seller never settled — money stayed in the wallet); <span class="tatt">agent-attested</span> when the run hasn't been chain-checked yet. Gateway-settled legs link straight to the explorer.</div>`
    : "";
  return `<div class="dstep"><div class="dhead"><span class="dnum">2</span> Spend under the cap — step by step</div>
    ${note}<ul class="tl">${items}</ul></div>`;
}

// ④ the decision the spend bought
function verdictCard(sym, v){
  const lv = String(v.verdict||"?").toLowerCase();
  const subs = (v.subtools||[]).map(s=>{
    const lvl = (s.level||"skipped");
    return `<div class="row"><span class="tn2">${esc(s.tool)}</span>
      <span class="lvl ${esc(lvl)}">${esc(lvl)}</span><span>${esc(s.reading||"")}</span></div>`;
  }).join("");
  return `<div class="vd"><div class="vhead"><span class="verd ${esc(lv)}">${esc(lv.toUpperCase())}</span>
    <b>${esc(sym)}</b> <span class="mut" style="font-size:11.5px">— safe to enter?</span></div>
    <div class="subt">${subs}</div></div>`;
}
function verdictCardFromFull(sym, v){
  const FT={liquidity:"orderbook_depth",carry:"funding_rates",crowding:"open_interest",security:"token_security"};
  const subs=Object.entries(v.factors||{}).map(([factor,f])=>{
    const lvl=(f||{}).level||"skipped";
    return `<div class="row"><span class="tn2">${esc(FT[factor]||factor)}</span>
      <span class="lvl ${esc(lvl)}">${esc(lvl)}</span><span>${esc((f||{}).reason||"")}</span></div>`;
  }).join("");
  const lv=String(v.verdict||"?").toLowerCase();
  return `<div class="vd"><div class="vhead"><span class="verd ${esc(lv)}">${esc(lv.toUpperCase())}</span> <b>${esc(sym)}</b></div><div class="subt">${subs}</div></div>`;
}
function whatCameBack(rz){
  if(!rz) return "";
  const kind = rz.kind || "pre_trade";
  const f = rz.findings||{};
  if(kind==="pre_trade"){
    const fv = (f.verdicts && Object.keys(f.verdicts).length) ? f.verdicts : null;
    let body = fv
      ? Object.entries(fv).map(([sym,v])=>verdictCard(sym,v)).join("")
      : Object.entries(rz.verdicts||{}).map(([sym,v])=>verdictCardFromFull(sym,v)).join("");
    const sk = Object.entries(rz.skipped||{}).map(([sym,why])=>`<div class="vd mut"><b>${esc(sym)}</b> skipped — ${esc(why)}</div>`).join("");
    if(!body && !sk) return "";
    const cap = `<div class="bought2">One $0.01 <code>pre_trade_check</code> fans out to order-book depth, funding, and open interest (plus a contract-security screen when there's a token) and returns one rules-based verdict — the synthesis the free calls don't do for you. It's a safety gate ("is it safe to enter"), not a buy signal.</div>`;
    return `<div class="dstep"><div class="dhead"><span class="dnum">3</span> What came back — the safety screen</div>${body}${sk}${cap}</div>`;
  }
  if(kind==="regime"){
    const r=f.regime||{};
    const cells=[
      r.fear_greed!=null? cell("Fear & Greed", esc(String(r.fear_greed)+(r.fear_greed_label?" · "+r.fear_greed_label:""))):"",
      r.funding_bias? cell("Funding", esc(r.funding_bias)):"",
      r.headlines!=null? cell("Headlines", esc(String(r.headlines)+(r.news_sentiment?" · net "+r.news_sentiment:""))):"",
      r.gas_gwei!=null? cell("ETH gas", esc(r.gas_gwei+" gwei")):"",
      r.defi_tvl_usd!=null? cell("DeFi TVL", humanUsd(r.defi_tvl_usd)):"",
      (r.defi_top&&r.defi_top.name)? cell("Largest protocol", esc(r.defi_top.name)+" · "+humanUsd(r.defi_top.tvl)):"",
    ].join("");
    if(!cells) return "";
    return `<div class="dstep"><div class="dhead"><span class="dnum">3</span> What came back — market regime</div><div class="readout">${cells}</div></div>`;
  }
  if(kind==="crowding"){
    const c=f.crowding||{};
    const rows=Object.entries(c).map(([sym,d])=>{
      const cells=[
        d.oi_usd!=null? cell("Open interest", humanUsd(d.oi_usd)):"",
        d.oi_change_24h_pct!=null? cell("OI 24h", esc((d.oi_change_24h_pct>0?"+":"")+d.oi_change_24h_pct+"%")):"",
        d.long_short_ratio!=null? cell("Long/short", esc(String(d.long_short_ratio))):"",
        d.spread_pct!=null? cell("Spread", esc(d.spread_pct+"%")):"",
      ].join("");
      return `<div class="vd"><b>${esc(sym)}</b><div class="readout">${cells}</div></div>`;
    }).join("");
    if(!rows) return "";
    const fb = f.funding_bias? `<div class="bought2">Funding bias across venues: ${esc(f.funding_bias)}.</div>`:"";
    return `<div class="dstep"><div class="dhead"><span class="dnum">3</span> What came back — perp positioning</div>${rows}${fb}</div>`;
  }
  if(kind==="vetting"){
    const vt=f.vetting||{}, rec=vt.recommendation||{}, cat=vt.catalog||{};
    if(!vt.vetting && !rec.name && cat.scanned==null) return "";
    const cells=[
      rec.name? cell("Recommended", esc(rec.name)) : "",
      rec.payers30d!=null? cell("Unique payers 30d", esc(String(rec.payers30d))):"",
      rec.calls30d!=null? cell("Calls 30d", esc(String(rec.calls30d))):"",
      cat.scanned!=null? cell("Catalog scanned", esc(String(cat.scanned))):"",
      cat.real_providers!=null? cell("Real providers", esc(String(cat.real_providers))):"",
      cat.sybil_collapsed!=null? cell("Sybils collapsed", esc(String(cat.sybil_collapsed))):"",
    ].join("");
    const big=(cat.biggest_factory&&cat.biggest_factory.listings)
      ? `<div class="bought2">Biggest sybil factory: ${esc(String(cat.biggest_factory.listings))} listings from one wallet${cat.biggest_factory.pay_to?` (${esc(String(cat.biggest_factory.pay_to).slice(0,10))}…)`:""}.</div>`:"";
    const capn=vt.vetting? `<div class="bought2">${esc(vt.vetting)}</div>`:"";
    if(!cells && !capn) return "";
    return `<div class="dstep"><div class="dhead"><span class="dnum">3</span> What came back — vetted marketplace</div>`
      +`<div class="vd"><div class="vhead"><span class="verd ok">VETTED</span> <b>verified_route</b> <span class="mut" style="font-size:11.5px">— the real, used provider (vet before you pay a stranger)</span></div></div>`
      +`${cells?`<div class="readout">${cells}</div>`:""}${capn}${big}</div>`;
  }
  if(kind==="strategy"){
    const sp=f.strategy_spec||{}, vt=f.vetting||{}, rec=vt.recommendation||{};
    const sig=sp.signal||{}, ex=sp.execution||{}, tok=(sp.universe&&sp.universe[0])||{};
    // ① the buyer-side trust step — what verified_route vetted
    const vet = (vt.vetting||rec.name)
      ? `<div class="vd"><div class="vhead"><span class="verd ok">VETTED</span> <b>marketplace (verified_route)</b></div>`
        + `<div class="subt">${esc(vt.vetting||"")}${rec.name?` &middot; pick <b>${esc(rec.name)}</b>${rec.payers30d!=null?" ("+esc(String(rec.payers30d))+" payers)":""}`:""}</div></div>`
      : "";
    // ② honest routing — paid only what isn't free
    const rt=(sp.data_provenance||[]).map(r=>{
      const cls = r.decision==="paid" ? "verd caution" : "verd ok";
      return `<div class="subt"><span class="verd ${cls.split(" ")[1]}">${esc(String(r.decision||"").toUpperCase())}</span> ${esc(r.need)} — ${esc(r.why||r.source||"")}</div>`;
    }).join("");
    const routing = rt? `<div class="vd"><div class="vhead"><b>Routing — pay only what isn't free</b></div>${rt}</div>`:"";
    // ③ the resulting backtestable spec
    const cells=[
      tok.symbol? cell("Token", esc(tok.symbol)+(tok.network?" · "+esc(tok.network):"")):"",
      sig.fear_greed!=null? cell("Fear & Greed", esc(String(sig.fear_greed))):"",
      sig.entry_bias? cell("Entry bias", esc(sig.entry_bias)):"",
      ex.liquidity_usd!=null? cell("Pool liquidity", humanUsd(ex.liquidity_usd)):"",
      ex.max_position_usd!=null? cell("Max position", money(ex.max_position_usd)):"",
    ].join("");
    // ④ the executed backtest — results, not just a spec template
    const bt=(sp.backtest&&sp.backtest.results)||null;
    let btCard="";
    if(bt){
      const bp=bt.best_params||{};
      const pct=v=>v==null?"—":(Number(v)>0?"+":"")+v+"%";
      const btCells=[
        cell("Return (180d)", pct(bt.total_return_pct)),
        cell("Sharpe", esc(String(bt.sharpe==null?"—":bt.sharpe))),
        cell("Max drawdown", bt.max_drawdown_pct==null?"—":"−"+bt.max_drawdown_pct+"%"),
        cell("Win rate", bt.win_rate_pct==null?"—":bt.win_rate_pct+"%"),
        cell("Trades", esc(String(bt.n_trades==null?"—":bt.n_trades))),
        cell("Exposure", bt.exposure_pct==null?"—":bt.exposure_pct+"%"),
      ].join("");
      const params=`fear ≤ ${esc(String(bp.fear_entry))} · exit ≥ ${esc(String(bp.greed_exit))} · hold ≤ ${esc(String(bp.hold_days_max))}d`;
      // vs buy-and-hold — the honest yardstick (risk-adjusted, not vs zero)
      const bm=bt.benchmark||null, ed=bt.edge||null;
      let vsHold="";
      if(bm && ed){
        const sign=v=>v==null?"—":(Number(v)>0?"+":"")+v;
        const winR=ed.beats_hold_return, winS=ed.beats_hold_sharpe, winD=ed.lower_drawdown;
        const verdict = (winS && (winR||winD))
          ? `<span class="verd ok">BEATS HOLD</span>`
          : (winR||winS||winD) ? `<span class="verd caution">MIXED vs HOLD</span>`
          : `<span class="verd avoid">TRAILS HOLD</span>`;
        vsHold=`<div class="subt" style="margin-top:9px">`
          +`<div class="row"><span class="tn2">vs buy &amp; hold</span>${verdict}</div>`
          +`<div class="row"><span class="tn2">return</span><span>${sign(bt.total_return_pct)}% strategy · ${sign(bm.total_return_pct)}% hold <b style="color:var(--fg)">(${sign(ed.excess_return_pct)}% edge)</b></span></div>`
          +`<div class="row"><span class="tn2">Sharpe</span><span>${esc(String(bt.sharpe))} strategy · ${esc(String(bm.sharpe))} hold <b style="color:var(--fg)">(${sign(ed.excess_sharpe)} edge)</b></span></div>`
          +`<div class="row"><span class="tn2">max drawdown</span><span>${esc(String(bt.max_drawdown_pct))}% strategy · ${esc(String(bm.max_drawdown_pct))}% hold <b style="color:var(--fg)">(${sign(ed.drawdown_reduction_pct)}% safer)</b></span></div>`
          +`</div>`;
      }
      btCard=`<div class="vd"><div class="vhead"><span class="verd ok">BACKTESTED</span> <b>best params</b>`
        +`<span class="mut" style="font-size:11.5px">— ${params} · ${esc(String(bt.combos_tested||0))} combos swept on ${esc(String(bt.bars||0))} daily bars</span></div>`
        +`<div class="readout">${btCells}</div>${vsHold}</div>`;
    }
    const rule = sig.rule? `<div class="bought2">Rule (${bt?"backtested on 180d history":"backtestable"}, not live trading): ${esc(sig.rule)}</div>`:"";
    if(!vet && !routing && !cells && !btCard) return "";
    return `<div class="dstep"><div class="dhead"><span class="dnum">3</span> What came back — ${esc(sp.name||"strategy spec")}</div>${vet}${routing}${cells?`<div class="readout">${cells}</div>`:""}${btCard}${rule}</div>`;
  }
  return "";
}

async function run(){
  const sub=document.getElementById("sub"), kpis=document.getElementById("kpis"), runs=document.getElementById("runs");
  try{
    const r = await fetch("/ledger.json",{headers:{"Accept":"application/json"}});
    const d = await r.json();
    const t = d.totals||{};
    const baseW=(d.wallets&&d.wallets.base)||"";
    sub.innerHTML = `Agent: <b style="color:#c4d0dc">${esc(d.agent||"flagship analyst")}</b>`
      + (baseW? ` &middot; payer <code>${esc(shortHash(baseW))}</code>`:"")
      + ` &middot; cap <code>${money(d.run_cap_usdc)}</code>/run`;
    kpis.innerHTML = `
      <div class="kpi"><div class="n">${t.runs||0}</div><div class="l">runs</div></div>
      <div class="kpi"><div class="n">${t.paid_calls||0}</div><div class="l">paid data calls</div></div>
      <div class="kpi"><div class="n">${t.free_calls||0}</div><div class="l">free data calls</div></div>
      <div class="kpi"><div class="n ac">${money(t.spent_usdc)}</div><div class="l">total intel spent</div></div>
      <div class="kpi" title="of settled spend ${money(t.settled_spent_usdc||t.spent_usdc)}: gateway-settled ${money(t.gateway_verified_spent_usdc||t.verified_spent_usdc)} + chain-verified ${money(t.chain_verified_spent_usdc||'0')}; agent-attested (not yet checked) ${money(t.attested_spent_usdc||'0')}. Booked but never settled: ${money(t.unsettled_spent_usdc||'0')}"><div class="n">${(t.verified_share_of_settled||t.verified_share)!=null? Math.round(parseFloat(t.verified_share_of_settled||t.verified_share)*100)+'%' : '—'}</div><div class="l">of settled spend verified</div></div>`;

    if(!(d.runs&&d.runs.length)){ runs.innerHTML='<div class="msg">No completed runs recorded yet.</div>'; return; }
    runs.innerHTML = d.runs.map(run=>{
      const cap=Number(run.cap_usdc||0), spent=Number(run.spent_usdc||0);
      const pct = cap>0? Math.min(100, Math.round(spent/cap*100)) : 0;
      const capPill = run.under_cap? `<span class="pill cap">under cap</span>` : `<span class="pill over">over cap</span>`;
      const rz = run.reasoning;
      const ctx = rz && (rz.regime||rz.context)
        ? `<div class="ctx">${esc([rz.regime, rz.context].filter(Boolean).join("  ·  "))}</div>` : "";
      const receiptNote = run.paid_count>0
        ? `${run.paid_count} verifiable on-chain receipt${run.paid_count===1?"":"s"}`
        : `free run — no on-chain spend`;
      return `<div class="run">
        <h2>Run <span class="when">${esc(fmtWhen(run.started))}</span> ${capPill}</h2>
        <div class="goal"><span class="lbl">Asked</span>${goalText(rz, run)}</div>
        ${ctx}
        ${planStep(rz, run)}
        ${execStep(run)}
        ${whatCameBack(rz)}
        <div class="receipt">
          <div class="spendbar"><i style="width:${pct}%"></i></div>
          <div class="spendmeta">Receipt: <b>${money(run.spent_usdc)}</b> intel spent · <b>${money(run.remaining_usdc)}</b> left of the <b>${money(run.cap_usdc)}</b> cap · ${receiptNote}</div>
        </div>
      </div>`;
    }).join("");
  }catch(e){
    document.getElementById("runs").innerHTML='<div class="msg">Could not load the ledger.</div>';
  }
}
run();
</script></div></body></html>"""
