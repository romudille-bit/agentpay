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

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from gateway.config import settings
from gateway.services.supabase import sb_enabled, sb_headers

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
    """Parse a Postgres ISO timestamp (handles trailing Z / +00:00)."""
    if not value:
        return None
    s = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Postgres sometimes returns >6 fractional digits; trim to micros.
        try:
            head, _, tail = s.partition(".")
            frac = "".join(c for c in tail if c.isdigit())[:6]
            tzpart = tail[len(frac):] if len(tail) > 6 else ""
            dt = datetime.fromisoformat(f"{head}.{frac or '0'}{tzpart}")
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
        out_runs.append({
            "started": run["started"],
            "ended": run["ended"],
            "free_count": len(run["free_calls"]),
            "paid_count": len(run["paid_calls"]),
            "free_calls": run["free_calls"],
            "paid_calls": run["paid_calls"],
            "spent_usdc": f"{spent:.2f}",
            "cap_usdc": f"{run['cap_usdc']:.2f}",
            "under_cap": spent <= run["cap_usdc"],
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
<title>AgentPay — Flagship Ledger</title>
<style>
  :root{--bg:#0b0e11;--card:#13181d;--line:#222a31;--fg:#e7edf3;--mut:#8a97a6;
        --ok:#4ade80;--warn:#fbbf24;--ac:#c3f53c;--base:#4f7cff;--stellar:#f5c542}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:880px;margin:0 auto;padding:28px 18px 80px}
  h1{font-size:22px;margin:0 0 2px}
  .sub{color:var(--mut);font-size:13px;margin:0 0 20px}
  .sub code{background:#1a2128;border-radius:4px;padding:1px 5px;font-size:12px}
  .kpis{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 22px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;
       padding:12px 16px;flex:1;min-width:130px}
  .kpi .n{font-size:24px;font-weight:650;letter-spacing:-.5px}
  .kpi .l{color:var(--mut);font-size:12px;margin-top:2px}
  .kpi .n.ac{color:var(--ac)}
  .run{background:var(--card);border:1px solid var(--line);border-radius:12px;
       padding:14px 18px;margin-bottom:12px}
  .run h2{font-size:14px;margin:0;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .when{color:var(--mut);font-weight:400;font-size:12.5px}
  .pill{font-size:11px;border-radius:20px;padding:2px 9px;white-space:nowrap}
  .pill.cap{background:rgba(74,222,128,.12);color:var(--ok);border:1px solid #1f4a2f}
  .pill.over{background:rgba(251,191,36,.12);color:var(--warn);border:1px solid #4a3f1f}
  .spendbar{height:7px;background:#1c232a;border-radius:4px;overflow:hidden;margin:11px 0 4px}
  .spendbar i{display:block;height:100%;background:var(--ac)}
  .spendmeta{color:var(--mut);font-size:12px;margin-bottom:8px}
  .spendmeta b{color:var(--fg);font-weight:600}
  table{width:100%;border-collapse:collapse;margin-top:6px}
  th,td{text-align:left;padding:5px 8px;font-size:12.5px;border-bottom:1px solid #1a2128}
  th{color:var(--mut);font-weight:500}
  td.r,th.r{text-align:right}
  .chip{font-size:10.5px;border-radius:5px;padding:1px 6px}
  .chip.base{background:rgba(79,124,255,.14);color:var(--base)}
  .chip.stellar{background:rgba(245,197,66,.14);color:var(--stellar)}
  a{color:var(--ac);text-decoration:none}
  a:hover{text-decoration:underline}
  .free{color:var(--mut);font-size:12.5px;margin-top:8px}
  .msg{color:var(--mut);padding:30px 0;text-align:center}
  .foot{color:var(--mut);font-size:12px;margin-top:22px}
  .foot a{color:var(--mut);text-decoration:underline}
</style></head><body><div class="wrap">

<h1>AgentPay — Flagship Ledger</h1>
<p class="sub" id="sub">An autonomous agent managing its own budget on AgentPay, live.
Loading…</p>

<div class="kpis" id="kpis"></div>
<div id="runs"><div class="msg">Loading ledger…</div></div>

<p class="foot">
  Every paid call is a real USDC settlement on Base, verifiable on-chain. Free
  intel calls settle $0 on Stellar but still produce a receipt. Source of truth:
  the durable <code>payment_logs</code> ledger.<br>
  <a href="/ledger.json">/ledger.json</a> · <a href="https://github.com/romudille-bit/agentpay">github.com/romudille-bit/agentpay</a>
</p>

<script>
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money = s => "$"+Number(s||0).toFixed(2);
function fmtWhen(iso){ if(!iso) return ""; const d=new Date(iso);
  return d.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",timeZoneName:"short"}); }
function shortHash(h){ return h? h.slice(0,8)+"…"+h.slice(-6) : ""; }

async function run(){
  const sub=document.getElementById("sub"), kpis=document.getElementById("kpis"), runs=document.getElementById("runs");
  try{
    const r = await fetch("/ledger.json",{headers:{"Accept":"application/json"}});
    const d = await r.json();
    const t = d.totals||{};
    const baseW = (d.wallets&&d.wallets.base)||"";
    sub.innerHTML = esc(d.description||"") + (baseW? ` &middot; payer <code>${esc(shortHash(baseW))}</code>`:"");
    kpis.innerHTML = `
      <div class="kpi"><div class="n">${t.runs||0}</div><div class="l">runs</div></div>
      <div class="kpi"><div class="n">${t.paid_calls||0}</div><div class="l">paid verdicts</div></div>
      <div class="kpi"><div class="n">${t.free_calls||0}</div><div class="l">free intel calls</div></div>
      <div class="kpi"><div class="n ac">${money(t.spent_usdc)}</div><div class="l">total USDC spent</div></div>`;

    if(!(d.runs&&d.runs.length)){ runs.innerHTML='<div class="msg">No completed flagship runs recorded yet.</div>'; return; }
    runs.innerHTML = d.runs.map(run=>{
      const cap=Number(run.cap_usdc||0), spent=Number(run.spent_usdc||0);
      const pct = cap>0? Math.min(100, Math.round(spent/cap*100)) : 0;
      const capPill = run.under_cap
        ? `<span class="pill cap">under cap</span>`
        : `<span class="pill over">over cap</span>`;
      const paidRows = (run.paid_calls||[]).map(p=>{
        const link = p.explorer_url? `<a href="${esc(p.explorer_url)}" target="_blank" rel="noopener">${esc(shortHash(p.tx_hash))} ↗</a>` : esc(shortHash(p.tx_hash));
        return `<tr><td>${esc(p.tool)}</td>
          <td><span class="chip ${esc(p.network)}">${esc(p.network)}</span></td>
          <td class="r">${money(p.amount_usdc)}</td>
          <td class="r">${link}</td></tr>`;
      }).join("");
      const freeNote = run.free_count
        ? `<div class="free">+ ${run.free_count} free intel call${run.free_count>1?"s":""} ($0, Stellar) — ${esc((run.free_calls||[]).map(f=>f.tool).join(", "))}</div>`
        : "";
      const paidTable = run.paid_count
        ? `<table><thead><tr><th>Paid call</th><th>Chain</th><th class="r">Cost</th><th class="r">On-chain</th></tr></thead><tbody>${paidRows}</tbody></table>`
        : "";
      return `<div class="run">
        <h2>Run <span class="when">${esc(fmtWhen(run.started))}</span> ${capPill}</h2>
        <div class="spendbar"><i style="width:${pct}%"></i></div>
        <div class="spendmeta"><b>${money(run.spent_usdc)}</b> spent of <b>${money(run.cap_usdc)}</b> cap
          · ${run.paid_count} paid · ${run.free_count} free</div>
        ${paidTable}${freeNote}
      </div>`;
    }).join("");
  }catch(e){
    document.getElementById("runs").innerHTML='<div class="msg">Could not load the ledger.</div>';
  }
}
run();
</script></div></body></html>"""
