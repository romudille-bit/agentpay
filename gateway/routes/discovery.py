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
from fastapi.responses import Response

import registry

from gateway._limiter import limiter
from gateway import radar
from gateway.config import GATEWAY_URL, settings, stellar_caip2

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
        result = radar.rank_from_payload(data, need, budget_dec, chain=chain)
    except Exception as e:
        logger.exception("Radar: ranking failed: %s", e)
        raise HTTPException(status_code=500, detail="discovery ranking failed")

    _cache_put(key, result)
    return result


# ── Public leaderboard (the human "visibility" surface) ─────────────────────────
# Self-contained HTML page that reads GET /discovery/arbitrum client-side and
# renders a curated, usage-ranked board of x402 tools on the Arbitrum stack.
# No build step, no external assets — served straight from the gateway.
_RADAR_LEADERBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arbitrum x402 Radar — AgentPay</title>
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
        "description": "The economic-intelligence layer for AI agents — hard budget caps at the payment layer, cost-aware routing before every call, and a verifiable receipt after. 17 tools free to start. USDC on Base or Stellar, no keys.",
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
        "description": "The economic-intelligence layer for AI agents — agents price a plan before spending, route to the cheapest tool that works, and stay under a hard budget cap, with a verifiable receipt after. 17 tools free to start. USDC on Base or Stellar, no keys.",
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
            "register_network": "stellar (free-tier identity; pay with your own funded wallet on Stellar or Base)",
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
        "description": "The economic-intelligence layer for AI agents — budget-capped tool calls with a verifiable receipt on every call. 17 tools free to start. USDC on Base or Stellar, no keys.",
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
        "description": "The economic-intelligence layer for AI agents — budget-capped x402 spending with verifiable receipts. 17 tools free to start. USDC on Base or Stellar.",
        "accepts": [
            {
                "scheme": "exact",
                "network": stellar_caip2(),
                "asset": "USDC",
                "assetIssuer": settings.USDC_ISSUER_MAINNET,
                "minAmount": str(min(prices)),
                "maxAmount": str(max(prices)),
                "facilitator": settings.STELLAR_FACILITATOR_URL,
            },
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base mainnet
                "minAmount": str(min(prices)),
                "maxAmount": str(max(prices)),
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


@router.get("/robots.txt", response_class=Response)
async def robots():
    return Response(
        content=(
            "User-agent: *\n"
            "Allow: /\n"
            f"Sitemap: {GATEWAY_URL}/sitemap.xml\n"
        ),
        media_type="text/plain",
    )


@router.get("/llms.txt", response_class=Response)
async def llms_txt():
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

AgentPay gives agents a wallet, a budget cap, and the awareness to spend it well. An agent can onboard with zero humans and zero funding in three calls: register, discover, call. Free tools cost $0 and need no funded wallet, yet every call still produces a receipt. Paid tools (and metered inference, coming) use x402: a 402 challenge, USDC settlement, retry with proof, verified on-chain. Chain-agnostic — USDC on Stellar or Base (CCTP-bridged 1:1).

## Onboarding (zero human, zero funding)

1. POST /v1/agent/register → {{ wallet, session_token, free_tools }}  (free)
2. GET  /tools → list tools
3. POST /tools/{{name}}/call → {{ result, receipt }}  (free tools settle $0)

Paid anchors: POST /v1/session/create ($0.01, Bazaar-indexed) and pre_trade_check ($0.01 — one-call trade verdict: slippage at size, funding carry, OI crowding, security).
Price any multi-tool plan BEFORE spending: POST /v1/plan/estimate (free, no wallet).

## Gateway

- Production: {GATEWAY_URL}
- Chains: USDC on Stellar or Base (Base is the canonical paid chain; Stellar is supported and CCTP-bridged)
- Tools: {len(tools)} ({len([t for t in tools if float(t.price_usdc) == 0])} free)
- Protocol: x402-v2 (HTTP 402 → pay → retry)
- SDK: pip install agentpay-x402 — one-liner: `from agentpay import quickstart; s = quickstart(); print(s.call('token_price', {{'symbol':'ETH'}}).data['price_usd'])`  (Base support: `pip install "agentpay-x402[base]"`)

## Tools

{tool_lines}

## Integration

POST /tools/{{name}}/call with {{parameters, agent_address}}
On 402: free tools ($0.000) authorize without an on-chain tx; paid tools settle USDC on Stellar or Base, retry with X-Payment header.
Response: data is in result["result"]

## Docs

- README: {GATEWAY_URL}/
- MCP server: npx @romudille/agentpay-mcp
- npm: https://www.npmjs.com/package/@romudille/agentpay-mcp
- GitHub: https://github.com/romudille-bit/agentpay
- Glama MCP: https://glama.ai/mcp/servers/romudille-bit/agentpay
"""
    return Response(content=content, media_type="text/plain")


@router.get("/sitemap.xml", response_class=Response)
async def sitemap():
    tools = registry.list_tools()
    urls = [
        f"{GATEWAY_URL}/",
        f"{GATEWAY_URL}/tools",
        f"{GATEWAY_URL}/.well-known/agentpay.json",
        f"{GATEWAY_URL}/.well-known/agent.json",
        f"{GATEWAY_URL}/.well-known/x402",
        f"{GATEWAY_URL}/faucet/ui",
    ] + [f"{GATEWAY_URL}/tools/{t.name}" for t in tools]

    loc_tags = "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{loc_tags}
</urlset>"""
    return Response(content=xml, media_type="application/xml")
