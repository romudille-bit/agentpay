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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import hmac
import re

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from gateway.config import settings
from gateway.services.supabase import (
    fetch_flagship_runs,
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
}


def _purpose(tool: str | None) -> str:
    return _TOOL_PURPOSE.get(tool or "", f"call {tool}")


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


def _flagship_addresses() -> list[str]:
    raw = (settings.LEDGER_FLAGSHIP_ADDRESSES or "").strip()
    if raw:
        return [a.strip() for a in raw.split(",") if a.strip()]
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
                run["reasoning"] = {
                    "objective":  m.get("objective") or {},
                    "plan":       m.get("plan") or {},
                    "regime":     m.get("regime") or "",
                    "context":    m.get("context") or "",
                    "verdicts":   m.get("verdicts") or {},
                    "skipped":    m.get("skipped") or {},
                    "receipt":    m.get("receipt") or {},
                    "free_intel": m.get("free_intel") or {},
                    "note":       m.get("note") or "",
                }
                used.add(i)
                enriched += 1
                break
    return enriched


async def _fetch_flagship_rows() -> list[dict]:
    """Read completed payment_logs rows for the flagship's wallet allowlist."""
    if not sb_enabled():
        return []
    addrs = _flagship_addresses()
    # Case-insensitive OR over the allowlist; PostgREST `or=(...)` syntax.
    or_clause = "(" + ",".join(f"agent_address.ilike.{a}" for a in addrs) + ")"
    params = {
        "select": "created_at,tool_name,network,amount_usdc,state,tx_hash,agent_address",
        "state": "eq.payment_done",
        "or": or_clause,
        "order": "created_at.asc",
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
        return resp.json()
    except Exception as e:
        logger.error(f"ledger fetch failure: {e}")
        return []


@router.get("/ledger.json", response_class=JSONResponse)
async def ledger_json():
    """Machine-readable flagship run history."""
    if not settings.LEDGER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    rows = await _fetch_flagship_rows()
    data = group_runs(rows, run_cap=settings.LEDGER_RUN_CAP_USDC)
    metas = await fetch_flagship_runs()
    data["runs_with_reasoning"] = attach_reasoning(data["runs"], metas)
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
    # No-store: the page should reflect fresh payment_logs on every load.
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})


@router.post("/v1/flagship/run")
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
    if not (x_flagship_secret and hmac.compare_digest(x_flagship_secret, secret)):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    stored = await insert_flagship_run(payload)
    # 200 when persisted; 202 when accepted-but-not-stored (e.g. table not yet
    # created) so the agent sees a 2xx and never fails its run over the ledger.
    return JSONResponse({"stored": stored}, status_code=200 if stored else 202)


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
<style>
  :root{--bg:#0b0e11;--card:#13181d;--line:#222a31;--fg:#e7edf3;--mut:#8a97a6;
        --ok:#4ade80;--warn:#fbbf24;--ac:#c3f53c;--base:#4f7cff;--stellar:#f5c542}
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
</style></head><body><div class="wrap">

<h1>AgentPay — How an Agent Decides What to Spend</h1>
<p class="lede"><b>AgentPay is the economic-intelligence layer for AI agents.</b> Below, an
autonomous market analyst runs live on it: every day it prices its plan <b>before</b>
paying, then executes step by step under a hard USDC budget it cannot exceed — and
every paid call leaves a verifiable on-chain receipt.</p>
<div class="howto"><b>THE DECISION LOOP</b><span class="arw">·</span>
  State a goal <span class="arw">→</span> Price the plan up front
  <span class="arw">→</span> Does it fit the budget? <span class="arw">→</span>
  Execute under the cap <span class="arw">→</span> Verifiable receipt</div>
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
  const o=(rz&&rz.objective)||{};
  const syms=Array.isArray(o.symbols)&&o.symbols.length? o.symbols.join(", ") : null;
  const cap=money(run.cap_usdc);
  if(syms && o.trade_size_usd)
    return `Decide whether ${esc(syms)} are safe to enter ${esc(o.side||"long")} at $${num(o.trade_size_usd)} notional — without exceeding a ${cap} budget.`;
  const vk = rz? Object.keys(rz.verdicts||{}).concat(Object.keys(rz.skipped||{})) : [];
  if(vk.length)
    return `Decide whether ${esc(vk.join(", "))} are safe to enter — without exceeding a ${cap} budget.`;
  return `Run the daily market check without exceeding a ${cap} budget.`;
}

// ② plan + the pre-flight price check (the AgentPay decision moment)
function planStep(rz, run){
  const plan=(rz&&rz.plan)||{};
  const steps=Array.isArray(plan.steps)? plan.steps.length : (run.timeline||[]).length;
  if(plan.total_usdc!=null){
    const fit = plan.fits_budget===false
      ? `<span class="verd caution">trimmed to fit</span>`
      : `<span class="verd ok">fits the ${money(run.cap_usdc)} cap ✓</span>`;
    return `<div class="dstep"><div class="dhead"><span class="dnum">1</span> Price the plan — before paying</div>
      <div class="pexpl">Estimated all <b>${steps} calls</b> at <b>${money(plan.total_usdc)}</b> up front via <code>/v1/plan/estimate</code>, then checked it against the budget: ${fit}</div></div>`;
  }
  return `<div class="dstep"><div class="dhead"><span class="dnum">1</span> Plan the run</div>
    <div class="pexpl"><b>${steps} calls</b> planned — ${run.free_count} free intel + ${run.paid_count} paid verdict${run.paid_count===1?"":"s"}, under a ${money(run.cap_usdc)} cap.</div></div>`;
}

// ③ execute the plan step by step, budget drawing down
function execStep(run){
  const items=(run.timeline||[]).map(s=>{
    const cost = s.kind==="paid"? money(s.cost_usdc) : "free";
    const link = (s.kind==="paid"&&s.explorer_url)
      ? `<a class="tlink" href="${esc(s.explorer_url)}" target="_blank" rel="noopener">tx ↗</a>` : "";
    return `<li>
      <span class="tn">${s.step}</span>
      <div class="tlmain"><span class="tpurpose">${esc(s.purpose)}</span><span class="ttool">${esc(s.tool)}</span></div>
      <span class="tcost ${esc(s.kind)}">${esc(cost)}</span>
      <span class="tbud">${money(s.remaining_usdc)} left</span>
      ${link}</li>`;
  }).join("");
  if(!items) return "";
  return `<div class="dstep"><div class="dhead"><span class="dnum">2</span> Run it — step by step, under the cap</div>
    <ul class="tl">${items}</ul></div>`;
}

// ④ the decision the spend bought
function decideStep(rz){
  if(!rz) return "";
  const verds = Object.entries(rz.verdicts||{}).map(([sym,v])=>{
    const factors=v.factors||{};
    const worst=Object.entries(factors).filter(([n,f])=>["caution","avoid"].includes((f||{}).level)).map(([n,f])=>`${esc(n)}: ${esc((f||{}).reason||"")}`);
    const lv=String(v.verdict||"?").toLowerCase();
    const detail=worst.length?` <span class="mut">(${worst.join("; ")})</span>`:` <span class="mut">(all factors clear)</span>`;
    return `<div class="vd"><span class="verd ${esc(lv)}">${esc(lv.toUpperCase())}</span> <b>${esc(sym)}</b>${detail}</div>`;
  }).join("");
  const sk = Object.entries(rz.skipped||{}).map(([sym,why])=>`<div class="vd mut"><b>${esc(sym)}</b> skipped — ${esc(why)}</div>`).join("");
  if(!verds && !sk) return "";
  return `<div class="dstep"><div class="dhead"><span class="dnum">3</span> Decide — what the spend bought</div><div class="verds">${verds}${sk}</div></div>`;
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
      <div class="kpi"><div class="n">${t.runs||0}</div><div class="l">decision cycles</div></div>
      <div class="kpi"><div class="n">${t.paid_calls||0}</div><div class="l">paid verdicts</div></div>
      <div class="kpi"><div class="n">${t.free_calls||0}</div><div class="l">free intel calls</div></div>
      <div class="kpi"><div class="n ac">${money(t.spent_usdc)}</div><div class="l">total USDC spent</div></div>`;

    if(!(d.runs&&d.runs.length)){ runs.innerHTML='<div class="msg">No completed decision cycles recorded yet.</div>'; return; }
    runs.innerHTML = d.runs.map(run=>{
      const cap=Number(run.cap_usdc||0), spent=Number(run.spent_usdc||0);
      const pct = cap>0? Math.min(100, Math.round(spent/cap*100)) : 0;
      const capPill = run.under_cap? `<span class="pill cap">stayed under cap</span>` : `<span class="pill over">over cap</span>`;
      const rz = run.reasoning;
      const ctx = rz && (rz.regime||rz.context)
        ? `<div class="ctx">${esc([rz.regime, rz.context].filter(Boolean).join("  ·  "))}</div>` : "";
      return `<div class="run">
        <h2>Decision cycle <span class="when">${esc(fmtWhen(run.started))}</span> ${capPill}</h2>
        <div class="goal"><span class="lbl">Goal</span>${goalText(rz, run)}</div>
        ${ctx}
        ${planStep(rz, run)}
        ${execStep(run)}
        ${decideStep(rz)}
        <div class="receipt">
          <div class="spendbar"><i style="width:${pct}%"></i></div>
          <div class="spendmeta">Receipt: <b>${money(run.spent_usdc)}</b> spent · <b>${money(run.remaining_usdc)}</b> left of the <b>${money(run.cap_usdc)}</b> cap · ${run.paid_count} verifiable on-chain receipt${run.paid_count===1?"":"s"}</div>
        </div>
      </div>`;
    }).join("");
  }catch(e){
    document.getElementById("runs").innerHTML='<div class="msg">Could not load the ledger.</div>';
  }
}
run();
</script></div></body></html>"""
