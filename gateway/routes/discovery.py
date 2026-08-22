"""
routes/discovery.py — Discovery + manifest endpoints.

  GET /.well-known/agentpay.json       — AgentPay manifest
  GET /.well-known/agent.json          — A2A protocol agent card
  GET /.well-known/l402-services       — 402index.io discovery format
  GET /.well-known/x402                — x402 protocol manifest
  GET /.well-known/402index-verify.txt — 402index.io domain proof
  GET /robots.txt                      — search-engine policy
  GET /llms.txt                        — LLM-readable service description
  GET /sitemap.xml                     — sitemap covering all public URLs
"""

import logging
import time
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import registry

from gateway._limiter import limiter
from gateway import radar
from gateway.config import GATEWAY_URL, settings, stellar_caip2
from gateway.guides import GUIDES, render_guide, render_guides_index

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Arbitrum x402 Radar ─────────────────────────────────────────────────────────
# Curated, usage-ranked discovery scoped to the Arbitrum stack (Arbitrum One +
# Sepolia + Robinhood Chain). Reuses the buyer-side router pipeline in
# gateway/radar.py. Bazaar discovery is fetched async (httpx) and ranked by the
# pure functions; results are cached briefly so a leaderboard refresh or a burst
# of agents doesn't hammer the CDP endpoint.
#
# Cache is BOUNDED: this is a public, unauthenticated endpoint and the key is
# attacker-controlled (need, budget, chain), so an unbounded dict would be a
# memory-growth/DoS vector. Expired entries are swept on write and the dict is
# capped at _RADAR_CACHE_MAX (oldest evicted).
_RADAR_CACHE: dict[tuple, tuple[float, dict]] = {}
_RADAR_TTL_SECS = 120
_RADAR_CACHE_MAX = 256


def _cache_get(key: tuple) -> dict | None:
    hit = _RADAR_CACHE.get(key)
    if not hit:
        return None
    if time.monotonic() - hit[0] >= _RADAR_TTL_SECS:
        _RADAR_CACHE.pop(key, None)  # purge stale on access
        return None
    return hit[1]


def _cache_put(key: tuple, value: dict) -> None:
    now = time.monotonic()
    # Sweep expired entries first.
    for k in [k for k, (ts, _) in _RADAR_CACHE.items() if now - ts >= _RADAR_TTL_SECS]:
        _RADAR_CACHE.pop(k, None)
    # Bound size: evict the oldest if still at cap.
    if len(_RADAR_CACHE) >= _RADAR_CACHE_MAX:
        oldest = min(_RADAR_CACHE, key=lambda k: _RADAR_CACHE[k][0])
        _RADAR_CACHE.pop(oldest, None)
    _RADAR_CACHE[key] = (now, value)


async def _fetch_bazaar_async(need: str) -> dict:
    """Async Bazaar discovery fetch (httpx) — avoids blocking the event loop."""
    import urllib.parse
    url = f"{radar.BAZAAR_URL}?query={urllib.parse.quote(need)}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers={"User-Agent": radar.UA, "Accept": "application/json"})
        r.raise_for_status()
        return r.json()


@router.get("/discovery/arbitrum")
@limiter.limit("30/minute")
async def discovery_arbitrum(
    request: Request,
    need: str = Query("", description="What the agent needs, e.g. 'funding rates'"),
    budget: float = Query(0.01, ge=0, description="Max USDC the agent will pay"),
    chain: str = Query("arbitrum-stack",
                       description="arbitrum-stack | arbitrum | arbitrum-sepolia | robinhood"),
):
    """Curated x402 discovery for the Arbitrum stack.

    Returns ranked, junk-filtered candidates (and a single recommendation) for
    `need` under `budget`, scoped to `chain`. This is the agent-facing surface;
    the public leaderboard reads the same endpoint.
    """
    if not settings.RADAR_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        budget_dec = Decimal(str(budget))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="invalid budget")

    key = (need.strip().lower(), str(budget_dec), chain.strip().lower())
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        if settings.RADAR_DEMO_FIXTURE:
            # Demo mode — serve a captured Bazaar payload (deterministic, offline).
            import json as _json
            import pathlib as _pathlib
            data = _json.loads(_pathlib.Path(settings.RADAR_DEMO_FIXTURE).read_text())
        else:
            data = await _fetch_bazaar_async(need)
    except Exception as e:
        # Log details server-side; return a generic message (don't leak upstream URL/error).
        logger.warning("Radar: discovery fetch failed: %s", e)
        raise HTTPException(status_code=502, detail="discovery upstream unavailable")

    if not isinstance(data, dict):
        logger.warning("Radar: Bazaar returned non-dict payload: %s", type(data).__name__)
        raise HTTPException(status_code=502, detail="discovery upstream returned unexpected payload")

    try:
        # Prober delivery scores (AGE-7): best-effort — {} = every service
        # unprobed = neutral factor, so a Supabase blip never breaks discovery.
        from gateway.services.supabase import fetch_service_scores
        scores = await fetch_service_scores()
        result = radar.rank_from_payload(data, need, budget_dec, chain=chain,
                                         scores=scores)
    except Exception as e:
        logger.exception("Radar: ranking failed: %s", e)
        raise HTTPException(status_code=500, detail="discovery ranking failed")

    _cache_put(key, result)
    return result


# ── Radar settlement verification ────────────────────────────────────────────
# The third act of the Radar flow: discover → settle via RadarSplit → VERIFY.
# Confirms the on-chain `Settled` event matches what the caller claims
# (canonical contract, paymentId, payer, developer, amount) and consumes the
# tx hash so one settlement can't be presented twice.

_RADAR_CHAINS: dict[str, tuple[str, str]] = {
    "arbitrum":         ("RADAR_CONTRACT_ARBITRUM", "RADAR_RPC_ARBITRUM"),
    "arbitrum-sepolia": ("RADAR_CONTRACT_ARBITRUM_SEPOLIA", "RADAR_RPC_ARBITRUM_SEPOLIA"),
    "robinhood":        ("RADAR_CONTRACT_ROBINHOOD", "RADAR_RPC_ROBINHOOD"),
}

# In-memory consume of verified radar txs (Supabase replay_tx_hashes is the
# durable layer, keyed network=radar-<chain>).
_consumed_radar_txs: set[str] = set()


class RadarVerifyRequest(BaseModel):
    tx_hash: str
    payment_id: str           # bytes32 hex the settle was issued with
    payer: str                # agent 0x address
    developer: str            # listed project's pay_to
    amount_usdc: str          # required total, e.g. "0.01"
    chain: str = "arbitrum-sepolia"


@router.post("/discovery/arbitrum/verify")
@limiter.limit("30/minute")
async def radar_verify(body: RadarVerifyRequest, request: Request):
    """Verify a RadarSplit settlement on-chain and consume it (one-shot)."""
    if not settings.RADAR_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    chain = body.chain.strip().lower()
    attrs = _RADAR_CHAINS.get(chain)
    if not attrs:
        raise HTTPException(status_code=422, detail=f"unknown chain '{body.chain}'")
    contract = getattr(settings, attrs[0])
    rpc_url  = getattr(settings, attrs[1])
    if not contract or not rpc_url:
        raise HTTPException(
            status_code=503,
            detail=f"Radar settlement verification not configured for '{chain}'",
        )

    tx_hash = body.tx_hash.strip().lower()
    network_label = f"radar-{chain}"

    # Replay pre-check (Supabase primary, in-memory fallback)
    from gateway.services import supabase as sb
    if tx_hash in _consumed_radar_txs or (
        sb.sb_enabled() and await sb.is_tx_hash_consumed(tx_hash, network_label)
    ):
        return {"success": False, "reason": "already_verified (replay)", "tx_hash": tx_hash}

    from gateway.base import usdc_to_atomic
    from gateway.radar_settle import verify_radar_settlement
    try:
        required_atomic = int(usdc_to_atomic(body.amount_usdc))
    except Exception:
        raise HTTPException(status_code=422, detail=f"unparseable amount_usdc {body.amount_usdc!r}")

    result = await verify_radar_settlement(
        tx_hash=tx_hash,
        contract=contract,
        payment_id=body.payment_id,
        payer=body.payer,
        developer=body.developer,
        required_amount_atomic=required_atomic,
        rpc_url=rpc_url,
    )

    if result["success"]:
        # Atomic consume — same pattern as the x402 paths: in-memory
        # check-and-add, then awaited durable insert (409 = lost the race).
        if tx_hash in _consumed_radar_txs:
            return {"success": False, "reason": "already_verified (replay)", "tx_hash": tx_hash}
        _consumed_radar_txs.add(tx_hash)
        recorded = await sb.record_tx_hash(tx_hash, network_label)
        if recorded is False:
            return {"success": False, "reason": "already_verified (replay)", "tx_hash": tx_hash}
        if recorded is None:
            # AGE-60 fail-closed: durable consume unconfirmed — reject
            # retryably and release the in-memory hold.
            _consumed_radar_txs.discard(tx_hash)
            return {"success": False, "tx_hash": tx_hash,
                    "reason": ("replay_check_unavailable: durable replay store "
                               "unreachable — retry the same tx_hash")}

    return {**result, "chain": chain, "contract": contract}


# ── Public leaderboard (the human "visibility" surface) ─────────────────────────
# Self-contained HTML page that reads GET /discovery/arbitrum client-side and
# renders a curated, usage-ranked board of x402 tools on the Arbitrum stack.
# No build step, no external assets — served straight from the gateway.
_RADAR_LEADERBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arbitrum x402 Radar — AgentPay</title>
<meta name="description" content="Curated, usage-ranked discovery of x402 services on the Arbitrum stack (Arbitrum One, Sepolia, Robinhood Chain). Junk-filtered, sybil-aware, with ready-to-pay details.">
<link rel="canonical" href="https://agentpay.tools/radar">
<style>
  :root{--bg:#0b0e11;--card:#13181d;--line:#222a31;--fg:#e7edf3;--mut:#8a97a6;--ac:#c3f53c;--ac2:#5ad1ff}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:980px;margin:0 auto;padding:28px 18px 60px}
  h1{font-size:24px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 22px}
  .controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
  input,select,button{background:var(--card);color:var(--fg);border:1px solid var(--line);
    border-radius:9px;padding:9px 11px;font-size:14px}
  input#need{flex:1;min-width:200px}
  button{background:var(--ac);color:#0b0e11;border:none;font-weight:700;cursor:pointer}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}
  .chip{font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:20px;
    padding:4px 10px;cursor:pointer;background:transparent}
  .chip:hover{color:var(--fg);border-color:var(--ac)}
  .rec{background:linear-gradient(180deg,#16201a,#13181d);border:1px solid #2c4a1f;
    border-radius:12px;padding:14px 16px;margin-bottom:16px}
  .rec .tag{color:var(--ac);font-size:12px;font-weight:700;letter-spacing:.04em}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  td.r,th.r{text-align:right}
  .net{font-size:11px;color:var(--ac2);border:1px solid #1f3a45;border-radius:6px;padding:2px 6px;white-space:nowrap}
  .name{font-weight:600}.url{color:var(--mut);font-size:12px;word-break:break-all}
  .msg{color:var(--mut);padding:18px 2px}
  a{color:var(--ac2)}.foot{color:var(--mut);font-size:12px;margin-top:22px;border-top:1px solid var(--line);padding-top:14px}
</style></head><body><div class="wrap">
  <h1>Arbitrum x402 Radar</h1>
  <p class="sub">The curated discovery layer for x402 tools on the Arbitrum stack —
    Arbitrum One, Sepolia, and Robinhood Chain. Usage-ranked, stub-filtered.
    Listed projects get paid at <b>0% gateway fee</b>.</p>
  <div class="controls">
    <input id="need" placeholder="What do you need? e.g. funding rates" value="funding rates">
    <select id="chain">
      <option value="arbitrum-stack">Arbitrum stack (all)</option>
      <option value="arbitrum">Arbitrum One</option>
      <option value="arbitrum-sepolia">Arbitrum Sepolia</option>
      <option value="robinhood">Robinhood Chain</option>
    </select>
    <input id="budget" type="number" step="0.001" min="0" value="0.01" style="width:96px" title="max USDC">
    <button id="go">Search</button>
  </div>
  <div class="chips" id="chips"></div>
  <div id="rec"></div>
  <div id="out" class="msg">Loading…</div>
  <div class="foot">Powered by AgentPay buyer-side routing ·
    <a href="/discovery/arbitrum?need=funding%20rates&chain=arbitrum-stack">JSON API</a> ·
    advise-only, no payment happens here.</div>
</div>
<script>
const EX = ["funding rates","token security","token price","defi tvl","crypto news"];
const chips = document.getElementById("chips");
EX.forEach(q => { const b=document.createElement("span"); b.className="chip"; b.textContent=q;
  b.onclick=()=>{document.getElementById("need").value=q; run();}; chips.appendChild(b); });
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function row(r,i){return `<tr><td class="r">${i+1}</td>
  <td><div class="name">${esc(r.name)}</div><div class="url">${esc(r.url)}</div></td>
  <td><span class="net">${esc(r.network)}</span></td>
  <td class="r">${r.price_usd==null?"?":"$"+esc(r.price_usd)}</td>
  <td class="r">${r.payers30d}/${r.calls30d}</td>
  <td class="r">${r.quality}</td></tr>`;}
async function run(){
  const need=document.getElementById("need").value||"";
  const chain=document.getElementById("chain").value;
  const budget=document.getElementById("budget").value||"0.01";
  const out=document.getElementById("out"); const recd=document.getElementById("rec");
  out.className="msg"; out.textContent="Loading…"; recd.innerHTML="";
  try{
    const res=await fetch(`/discovery/arbitrum?need=${encodeURIComponent(need)}&chain=${chain}&budget=${budget}`);
    if(!res.ok){out.textContent="Discovery unavailable ("+res.status+"). Try again shortly.";return;}
    const d=await res.json(); const rows=d.results||[];
    if(d.recommendation){const r=d.recommendation;
      recd.innerHTML=`<div class="rec"><div class="tag">★ RECOMMENDED</div>
        <div style="margin-top:4px"><span class="name">${esc(r.name)}</span> —
        ${r.price_usd==null?"?":"$"+esc(r.price_usd)} on <span class="net">${esc(r.network)}</span></div>
        <div class="url">${esc(r.url)} · ${r.payers30d} payers / ${r.calls30d} calls</div></div>`;}
    if(!rows.length){out.className="msg";
      out.textContent = (chain.startsWith("robinhood")
        ? "Bazaar doesn't index Robinhood Chain — its tools appear via the AgentPay crawler."
        : "No real, affordable tools found for this query on "+chain+".");return;}
    out.className=""; out.innerHTML=`<table><thead><tr>
      <th class="r">#</th><th>Tool</th><th>Network</th><th class="r">Price</th>
      <th class="r">Payers/Calls</th><th class="r">Quality</th></tr></thead>
      <tbody>${rows.map(row).join("")}</tbody></table>`;
  }catch(e){out.className="msg";out.textContent="Could not reach discovery.";}
}
document.getElementById("go").onclick=run;
document.getElementById("need").addEventListener("keydown",e=>{if(e.key==="Enter")run();});
run();
</script></body></html>"""


@router.get("/radar", response_class=Response)
async def radar_leaderboard():
    """Public leaderboard for the Arbitrum x402 Radar (reads /discovery/arbitrum)."""
    if not settings.RADAR_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=_RADAR_LEADERBOARD_HTML, media_type="text/html")


@router.get("/.well-known/agentpay.json")
async def well_known_agentpay():
    """AgentPay manifest — discoverable by x402-aware agents."""
    tools = registry.list_tools()
    return {
        "name": "AgentPay",
        "version": "1.0",
        "tagline": "The economic-intelligence layer for AI agents — spend control, not just a wallet.",
        "description": "The economic-intelligence layer for AI agents — hard budget caps at the payment layer, cost-aware routing before every call, and a verifiable receipt after. 17 tools free to start. USDC on Base (standard x402) or Stellar (via the AgentPay SDK), no keys.",
        "url": GATEWAY_URL,
        "payment_protocol": "x402",
        "payment_network": stellar_caip2(),
        "payment_asset": "USDC",
        "pricing_model": "per-call",
        "budget_aware": True,
        "faucet": f"{GATEWAY_URL}/faucet",
        "tools_endpoint": f"{GATEWAY_URL}/tools",
        "capabilities": ["market-data", "onchain-analytics", "defi", "sentiment", "whale-tracking"],
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "price_usdc": t.price_usdc,
                "category": t.category,
                "parameters": t.parameters,
                "endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "triggers": t.triggers,
                "use_when": t.use_when,
                "returns": t.returns,
                "response_example": t.response_example,
            }
            for t in tools
        ],
    }


@router.get("/.well-known/agent.json")
async def well_known_agent():
    """A2A protocol card — agent-to-agent discovery."""
    tools = registry.list_tools()
    free_tools  = [t for t in tools if float(t.price_usdc) == 0]
    paid_tools  = [t for t in tools if float(t.price_usdc) > 0]
    return {
        "name":        "AgentPay",
        "description": "The economic-intelligence layer for AI agents — agents price a plan before spending, route to the cheapest tool that works, and stay under a hard budget cap, with a verifiable receipt after. 17 tools free to start. USDC on Base (standard x402) or Stellar (via the AgentPay SDK), no keys.",
        "url":         GATEWAY_URL,
        "version":     "1.0",

        # ── How an agent onboards (free, zero human, zero funding) ───────────
        # Step 1: POST /v1/agent/register → mints a wallet + session_token,
        #         lists free tools. Free, no payment.
        # Step 2: GET  /tools → discover all tools (17 free + session_create)
        # Step 3: POST /tools/{name}/call → call any tool; free tools settle
        #         $0 (no funding needed) but still produce a receipt.
        # Paid path: POST /v1/session/create ($0.01) is the Bazaar-indexed
        #         paid anchor; agents that discover AgentPay on Bazaar land there.
        "onboarding": {
            "register":         f"{GATEWAY_URL}/v1/agent/register",
            "register_cost":    "0",
            "register_network": "stellar (free-tier identity; pay with your own funded wallet on Base — standard x402 — or Stellar via the AgentPay SDK)",
            "discover_tools":   f"{GATEWAY_URL}/tools",
            "call_tool":        f"{GATEWAY_URL}/tools/{{name}}/call",
            "paid_session":     f"{GATEWAY_URL}/v1/session/create",
            "sdk":              "pip install agentpay-x402",
            "sdk_quickstart":   "from agentpay import quickstart; s = quickstart(); s.call('token_price', {'symbol':'ETH'})",
        },

        "capabilities": {
            "tools":           True,
            "budget_sessions": True,
            "session_receipts": True,
            "payments":        "x402-v2",
            "networks":        ["stellar-mainnet", "base-mainnet"],
            "free_tools":      len(free_tools),
            "paid_tools":      len(paid_tools),
        },

        "contact": "https://github.com/romudille-bit/agentpay",

        "tools": [
            {
                "name":          t.name,
                "description":   t.description,
                "price_usdc":    t.price_usdc,
                "category":      t.category,
                "call_endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "triggers":      t.triggers,
                "use_when":      t.use_when,
                "returns":       t.returns,
            }
            for t in tools
        ],
    }


@router.get("/.well-known/l402-services")
async def well_known_l402_services():
    """402index.io discovery document — lists all paid endpoints with pricing and request schemas."""
    tools = registry.list_tools()

    def _request_body(tool) -> dict:
        """Convert JSON-Schema parameters to 402index request_body format."""
        props = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        return {
            field: {
                **spec,
                "required": field in required,
            }
            for field, spec in props.items()
        }

    return {
        "version": "0.2.0",
        "name": "AgentPay",
        "description": "The economic-intelligence layer for AI agents — budget-capped tool calls with a verifiable receipt on every call. 17 tools free to start. USDC on Base (standard x402) or Stellar (via the AgentPay SDK), no keys.",
        "homepage": GATEWAY_URL,
        "protocol": "x402",
        "protocols": ["x402"],
        "payment_network": stellar_caip2(),
        "services": [
            {
                "id": t.name,
                "name": t.name.replace("_", " ").title(),
                "description": t.description,
                "endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "method": "POST",
                "content_type": "application/json",
                "pricing": {
                    "amount": float(t.price_usdc),
                    "currency": "USD",
                    "type": "fixed",
                },
                "request_body": _request_body(t),
            }
            for t in tools
        ],
    }


@router.get("/.well-known/x402")
async def well_known_x402():
    """
    x402 protocol discovery manifest.
    Scanners and agents probing /.well-known/x402 find supported networks,
    assets, pricing range, and facilitator info here.
    """
    tools = registry.list_tools()
    prices = [float(t.price_usdc) for t in tools]
    return {
        "x402Version": 1,
        "gateway": GATEWAY_URL,
        "name": "AgentPay",
        "description": "The economic-intelligence layer for AI agents — budget-capped x402 spending with verifiable receipts. 17 tools free to start. USDC on Base (standard x402) or Stellar (via the AgentPay SDK).",
        "accepts": [
            # Base IS the standard x402 `exact` scheme (EIP-3009 via the CDP
            # facilitator) — any standard x402 client can pay here. Lead with it.
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base mainnet
                "minAmount": str(min(prices)),
                "maxAmount": str(max(prices)),
            },
            # AGE-128: this entry previously claimed scheme "exact" on
            # stellar CAIP-2 + the OZ facilitator. That was FALSE advertising:
            # our Stellar rail is a classic payment + text memo verified via
            # Horizon — NOT the standard @x402/stellar Soroban scheme (null-
            # account template, signed auth entries, facilitator settlement).
            # A standard client that trusted the old entry built a Soroban tx
            # we could never verify. Name the scheme honestly and say how to
            # actually pay.
            {
                "scheme": "agentpay-classic-memo",
                "network": stellar_caip2(),
                "asset": "USDC",
                "assetIssuer": settings.USDC_ISSUER_MAINNET,
                "minAmount": str(min(prices)),
                "maxAmount": str(max(prices)),
                "note": (
                    "Classic Stellar payment + text memo (payment_id), verified "
                    "via Horizon — not the standard @x402/stellar Soroban "
                    "scheme. Pay with the AgentPay SDK (pip install "
                    "agentpay-x402) or manually per the 402 instructions."
                ),
            },
        ],
        "endpoints": [
            {
                "path": f"/tools/{t.name}/call",
                "method": "POST",
                "amountRequired": t.price_usdc,
            }
            for t in tools
        ],
    }


@router.get("/.well-known/402index-verify.txt", response_class=Response)
async def well_known_402index_verify():
    """
    Domain-verification file for 402index.io.

    Serves the SHA-256 hash from INDEX402_VERIFY_HASH as plain text. Returns
    404 when the env var is empty so the endpoint is harmless until claimed.
    """
    if not settings.INDEX402_VERIFY_HASH:
        raise HTTPException(status_code=404, detail="Not configured")
    return Response(
        content=settings.INDEX402_VERIFY_HASH + "\n",
        media_type="text/plain",
    )


@router.get("/indexnow.txt", response_class=Response)
async def indexnow_key():
    """IndexNow key file (indexnow.org) — instant URL submission to Bing,
    Seznam, Naver, Yandex. The key content must match the INDEXNOW_KEY env
    var; submit URLs with keyLocation={GATEWAY_URL}/indexnow.txt, e.g.:

        curl "https://api.indexnow.org/indexnow?url=<page>&key=<key>&keyLocation=<GATEWAY_URL>/indexnow.txt"

    404 when unconfigured (same pattern as 402index-verify)."""
    if not settings.INDEXNOW_KEY:
        raise HTTPException(status_code=404, detail="Not configured")
    return Response(content=settings.INDEXNOW_KEY, media_type="text/plain")


@router.get("/robots.txt", response_class=Response)
async def robots():
    # Content-Signal (contentsignals.org): explicitly WELCOMING — AgentPay's
    # customers are AI agents, and presence in AI training/retrieval corpora
    # is distribution, not leakage. (Deliberate inverse of the Cloudflare
    # managed default, which we disabled 2026-07-11.)
    return Response(
        content=(
            "User-agent: *\n"
            "Allow: /\n"
            "Content-Signal: search=yes, ai-train=yes, ai-input=yes\n"
            f"Sitemap: {GATEWAY_URL}/sitemap.xml\n"
        ),
        media_type="text/plain",
    )


# ── Agent-readiness endpoints (RFC 9727 / auth.md / MCP server card) ──────────
# Machine-discoverable entry points for AI agents. AgentPay's auth model is
# x402 payment (no OAuth), so /auth.md documents registration + payment and we
# deliberately do NOT publish OAuth discovery metadata we don't have.

@router.get("/.well-known/api-catalog", response_class=JSONResponse)
async def api_catalog():
    """RFC 9727 API catalog (linkset+json)."""
    linkset = {
        "linkset": [{
            "anchor": f"{GATEWAY_URL}/",
            "service-desc": [{"href": f"{GATEWAY_URL}/openapi.json",
                              "type": "application/json"}],
            "service-doc":  [{"href": f"{GATEWAY_URL}/llms.txt",
                              "type": "text/plain"},
                             {"href": f"{GATEWAY_URL}/auth.md",
                              "type": "text/markdown"}],
            "service-meta": [{"href": f"{GATEWAY_URL}/.well-known/agentpay.json",
                              "type": "application/json"}],
            "status":       [{"href": f"{GATEWAY_URL}/health",
                              "type": "application/json"}],
        }]
    }
    return JSONResponse(content=linkset,
                        media_type="application/linkset+json",
                        headers={"Cache-Control": "public, max-age=3600"})


_AUTH_MD = f"""# AgentPay — agent access & authentication

There is **no OAuth, no API key, and no signup form**. AgentPay authenticates
agents the x402 way: by payment.

## Free tier (17 tools, $0)

1. Register (optional but recommended — mints an identity + session token):

       POST {GATEWAY_URL}/v1/agent/register

2. Call any free tool. You'll receive a `402` challenge; retry with the
   `free:<payment_id>` proof exactly as the challenge instructs. No wallet,
   no funds required.

## Paid tools ($0.01: session_create, pre_trade_check, verified_route)

Pay the `402` challenge with USDC on **Base** (EIP-3009, gasless) or
**Stellar**. The easiest client is the SDK:

    pip install "agentpay-x402[base]"

    from agentpay import quickstart
    s = quickstart(max_spend=0.10)          # hard budget cap
    r = s.call("token_price", {{"symbol": "ETH"}})

Every paid call returns a verifiable on-chain receipt (see `/ledger`).

## MCP

    npx @romudille/agentpay-mcp             # keyless, Node >= 18

## Machine-readable surfaces

- OpenAPI: {GATEWAY_URL}/openapi.json
- API catalog (RFC 9727): {GATEWAY_URL}/.well-known/api-catalog
- Manifest: {GATEWAY_URL}/.well-known/agentpay.json
- A2A card: {GATEWAY_URL}/.well-known/agent.json
- LLM guide: {GATEWAY_URL}/llms.txt
"""


@router.get("/auth.md", response_class=Response)
async def auth_md():
    """Agent registration/authentication guide (auth.md convention)."""
    return Response(content=_AUTH_MD, media_type="text/markdown",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/.well-known/mcp/server-card.json", response_class=JSONResponse)
async def mcp_server_card():
    """MCP Server Card (SEP-1649, schema still stabilizing) for the
    npm-distributed AgentPay MCP server."""
    return JSONResponse(content={
        "serverInfo": {"name": "agentpay-mcp", "version": "2.3.0"},
        "description": ("AgentPay x402 gateway as MCP tools: 17 free crypto/"
                        "web data tools, keyless vetted routing (verified_route "
                        "preview), and pre-flight plan pricing."),
        "transport": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@romudille/agentpay-mcp"],
            "runtime": "node>=18",
        },
        "capabilities": {"tools": True, "resources": False, "prompts": False},
        "homepage": GATEWAY_URL,
        "registry": "https://www.npmjs.com/package/@romudille/agentpay-mcp",
    }, headers={"Cache-Control": "public, max-age=3600"})


def build_llms_txt() -> str:
    """LLM/agent-readable service description in markdown.

    Shared by GET /llms.txt and by the root route's `Accept: text/markdown`
    content negotiation (gateway/routes/infra.py) — the origin-side equivalent
    of Cloudflare's paid "Markdown for Agents" feature.
    """
    tools = registry.list_tools()
    def _price_label(p: str) -> str:
        try:
            return "Free" if float(p) == 0 else f"${p}"
        except (ValueError, TypeError):
            return f"${p}"

    tool_lines = "\n".join(
        f"- {t.name} ({_price_label(t.price_usdc)}): {t.description}"
        for t in sorted(tools, key=lambda t: t.name)
    )
    content = f"""\
# AgentPay

> The economic intelligence layer for agent spend. An agent reasons about cost — prices a plan before spending and routes to the cheapest tool that works — under a hard budget cap enforced before a dollar moves. 17 free tools to start: no API keys, no USDC, no wallet setup. Every call is session-tracked with a full receipt.

AgentPay gives agents a wallet, a budget cap, and the awareness to spend it well. An agent can onboard with zero humans and zero funding in three calls: register, discover, call. Free tools cost $0 and need no funded wallet, yet every call still produces a receipt. Paid tools (and metered inference, coming) use x402: a 402 challenge, USDC settlement, retry with proof, verified on-chain. USDC on Base (standard x402 `exact` scheme — any standard client can pay) or Stellar (classic payment + memo via the AgentPay SDK — not the standard @x402/stellar Soroban scheme); Circle CCTP bridges 1:1 between them.

## Onboarding (zero human, zero funding)

1. POST /v1/agent/register → {{ wallet, session_token, free_tools }}  (free)
2. GET  /tools → list tools
3. POST /tools/{{name}}/call → {{ result, receipt }}  (free tools settle $0)

Paid anchors (all $0.01, Bazaar-indexed): POST /v1/session/create (budget-capped session); pre_trade_check (one-call trade verdict — slippage at size, funding carry, OI crowding, security); verified_route (buyer-side trust oracle — sweeps the x402 marketplace, collapses sybils, returns one vetted, ready-to-pay provider).
Price any multi-tool plan BEFORE spending: POST /v1/plan/estimate (free, no wallet).

## Gateway

- Production: {GATEWAY_URL}
- Chains: USDC on Base (canonical paid chain; standard x402 `exact` scheme) or Stellar (classic payment + memo via the AgentPay SDK — not the standard @x402/stellar Soroban scheme; CCTP-bridged 1:1)
- Tools: {len(tools)} ({len([t for t in tools if float(t.price_usdc) == 0])} free)
- Protocol: x402-v2 (HTTP 402 → pay → retry)
- SDK: pip install agentpay-x402 — one-liner: `from agentpay import quickstart; s = quickstart(); print(s.call('token_price', {{'symbol':'ETH'}}).data['price_usd'])`  (Base support: `pip install "agentpay-x402[base]"`)

## Tools

{tool_lines}

## Integration

POST /tools/{{name}}/call with {{parameters, agent_address}}
On 402: free tools ($0.000) authorize without an on-chain tx; paid tools settle USDC on Base (standard x402, PAYMENT-SIGNATURE header) or Stellar (AgentPay SDK / manual classic payment + memo, X-Payment header).
Response: data is in result["result"]

## Docs

- README: {GATEWAY_URL}/
- Agent Skills: npx skills add romudille-bit/agentpay — installs the agentpay-route
  (vet + pay the best x402 tool under budget) and agentpay-session (spend cap +
  receipts) skills into Claude Code, Codex, Droid, OpenCode, and other
  skills-CLI-compatible runtimes.
- MCP server: npx @romudille/agentpay-mcp — keyless: 17 free tools + verified_route
  preview. Wallet mode: set AGENTPAY_BASE_KEY (EVM key) + AGENTPAY_MAX_SPEND to
  settle paid tools in-place (gasless EIP-3009 on Base, hard session cap).
- npm: https://www.npmjs.com/package/@romudille/agentpay-mcp
- GitHub: https://github.com/romudille-bit/agentpay
- Glama MCP: https://glama.ai/mcp/servers/romudille-bit/agentpay
"""
    return content


@router.get("/llms.txt", response_class=Response)
async def llms_txt():
    return Response(content=build_llms_txt(), media_type="text/plain")


@router.get("/sitemap.xml", response_class=Response)
async def sitemap():
    from gateway.routes.prober import service_has_probe_data, service_slug
    from gateway.services.supabase import fetch_service_scores

    tools = registry.list_tools()
    try:                      # per-service SEO pages (AGE-39); [] on blip
        scores = await fetch_service_scores()
    except Exception:
        scores = {}
    # (url, lastmod|None) — lastmod only where we actually know it: /s/ pages
    # carry the row's updated_at (stamped on every Mon/Thu probe sweep).
    # Fabricating lastmod for static pages erodes crawler trust, so they omit it.
    def _lastmod(row: dict) -> str | None:
        ts = row.get("updated_at") or row.get("probed_at")
        return str(ts)[:10] if ts else None

    urls: list[tuple[str, str | None]] = [
        (f"{GATEWAY_URL}/", None),
        (f"{GATEWAY_URL}/tools", None),
        (f"{GATEWAY_URL}/probes", None),
        (f"{GATEWAY_URL}/ledger", None),
        (f"{GATEWAY_URL}/radar", None),
        (f"{GATEWAY_URL}/privacy", None),
        (f"{GATEWAY_URL}/guides", None),
        (f"{GATEWAY_URL}/.well-known/agentpay.json", None),
        (f"{GATEWAY_URL}/.well-known/agent.json", None),
        (f"{GATEWAY_URL}/.well-known/x402", None),
        # /faucet/ui 404s on mainnet (testnet-only) — a sitemap URL that 404s
        # is a standing crawl error, so only list it where it actually serves.
    ] + ([(f"{GATEWAY_URL}/faucet/ui", None)]
         if settings.STELLAR_NETWORK != "mainnet" else []) \
      + [(f"{GATEWAY_URL}/tools/{t.name}", None) for t in tools] \
      + [(f"{GATEWAY_URL}/s/{service_slug(u)}", _lastmod(scores[u]))
         # Only /s/ pages with real probe evidence enter the sitemap; the
         # unprobed majority are near-identical shells that would dilute the
         # domain (2026-08-22 — see service_has_probe_data). A page joins the
         # sitemap automatically on its first probe result, lastmod stamped.
         for u in sorted(scores) if service_has_probe_data(scores[u])] \
      + [(f"{GATEWAY_URL}/guides/{s}", g["published"])
         for s, g in sorted(GUIDES.items())]

    loc_tags = "\n".join(
        f"  <url><loc>{u}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>"
        for u, lm in urls
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{loc_tags}
</urlset>"""
    return Response(content=xml, media_type="application/xml")


# ── Guides — long-form technical content, server-rendered for search + answer
# engines. Authority accrues to the domain that serves the words, so these live
# on the gateway rather than a third-party blog. See gateway/guides.py.
@router.get("/guides", response_class=Response)
async def guides_index():
    return Response(content=render_guides_index(GATEWAY_URL), media_type="text/html")


@router.get("/guides/{slug}", response_class=Response)
async def guide_page(slug: str):
    if slug not in GUIDES:
        raise HTTPException(status_code=404, detail="Guide not found")
    return Response(content=render_guide(slug, GATEWAY_URL), media_type="text/html")


# ── Privacy policy — required for the Anthropic Connectors Directory + plugin
# listing ("missing or incomplete privacy policies result in immediate rejection").
# Plain string (not an f-string) so the CSS braces are literal.
_PRIVACY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentPay — Privacy Policy</title>
<style>
  :root{--bg:#0b0e11;--fg:#e7edf3;--mut:#8a97a6;--ac:#4ade80;--line:#222a31}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:16px/1.65 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:760px;margin:0 auto;padding:40px 20px 80px}
  h1{font-size:26px;margin:0 0 4px}
  h2{font-size:18px;margin:28px 0 8px;color:var(--ac)}
  .upd{color:var(--mut);font-size:13px;margin:0 0 8px}
  p,li{color:#cdd6df}
  a{color:var(--ac)} code{background:#1a2128;border-radius:4px;padding:1px 5px;font-size:13px}
  ul{padding-left:20px}
</style></head><body><div class="wrap">
<h1>AgentPay — Privacy Policy</h1>
<p class="upd">Last updated: 2026-06-18</p>
<p>AgentPay is an x402 payment gateway and economic-intelligence layer for AI agents. This policy
explains what data AgentPay processes when an agent (or its operator) uses our tools, MCP server,
SDK, or gateway at <code>agentpay.tools</code>. AgentPay is built for autonomous agents, not human
end users, and we do <b>not</b> collect names, emails, or other personal identifiers.</p>

<h2>What we process</h2>
<ul>
<li><b>Agent wallet address &amp; session identifiers</b> — the public Stellar/Base address and
session token, used to enforce budget caps, route payments, and produce receipts. We never receive
your private keys; payments are signed client-side.</li>
<li><b>Tool-call metadata</b> — the tool name, the parameters you send (e.g. a token symbol), the
amount, on-chain transaction hash, network, and timestamp — recorded in our usage/payment logs.</li>
<li><b>Network/technical data</b> — request IP address and user-agent, used for rate limiting and
abuse prevention.</li>
</ul>

<h2>On-chain data</h2>
<p>Payments settle on public blockchains (Stellar, Base). On-chain transactions — addresses,
amounts, and tx hashes — are public by nature and outside AgentPay's control. The public receipt
ledger (<code>/ledger</code>) displays only the AgentPay flagship agent's own on-chain activity.</p>

<h2>Third parties</h2>
<p>To fulfill a tool call, AgentPay forwards the necessary request parameters to upstream public
data providers (including CoinGecko, Binance, Bybit, OKX, CoinMarketCap, Etherscan, DeFiLlama,
GoPlus, Dune Analytics, alternative.me, Reddit, and Jina) and to payment facilitators (Coinbase
CDP for Base; Stellar Horizon). Each has its own privacy policy. Usage and payment logs are stored
with our database provider, Supabase. We do not sell data, and we do not use it for advertising.</p>

<h2>Storage &amp; retention</h2>
<p>Usage and payment logs are stored to operate the service (budget enforcement, receipts, and
aggregate analytics) and retained only as long as needed for those purposes and any legal or
accounting obligations.</p>

<h2>Your choices</h2>
<p>Free tools require no funded wallet and the MCP server can run with an ephemeral identity. You
control which tools you call and what parameters you send.</p>

<h2>Changes</h2>
<p>We may update this policy; material changes will be reflected by the "Last updated" date above.</p>

<h2>Contact</h2>
<p>Questions about this policy: <a href="mailto:romudille@gmail.com">romudille@gmail.com</a>.</p>
</div></body></html>"""


@router.get("/privacy", response_class=Response)
async def privacy():
    return Response(content=_PRIVACY_HTML, media_type="text/html")
