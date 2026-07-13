"""
routes/prober.py — Active Prober ingest (AGE-6).

POST /v1/prober/run — the prober posts its raw probe rows (plus an optional
flagship-style run summary for /ledger reasoning); the GATEWAY does the
storage work, because it holds the Supabase creds and the 30-day history:

    1. INSERT raw rows into service_probes (private evidence)
    2. SELECT the full 30d window back (this run + history)
    3. score() — the SAME pure function the runner uses — over the window
    4. UPSERT service_scores (public, consumed by decide() — AGE-7)
    5. optionally insert_flagship_run(run) so /ledger shows the reasoning

The prober stays a credential-free HTTP customer (flagship ingest pattern);
the secret gate is the same FLAGSHIP_INGEST_SECRET, reused per PROBER_SPEC.
"""

from __future__ import annotations

import hashlib
import hmac
import html as _html
import logging
import re

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from agents.prober.probe import score
from gateway.config import settings
from gateway.services.supabase import (
    fetch_service_probes,
    insert_flagship_run,
    insert_service_probes,
    upsert_service_scores,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def service_slug(url: str) -> str:
    """Stable, readable, collision-safe slug for a scored service URL.

    host-and-path words + 6-hex sha1 tail, e.g.
    https://api.exa.ai/search → api-exa-ai-search-1a2b3c. Pure; the same
    function feeds /scores.json ("page"), the /s/{slug} route, and the
    sitemap, so links can never drift apart. (AGE-39 SEO pages.)"""
    tail = hashlib.sha1(url.encode()).hexdigest()[:6]
    base = re.sub(r"^https?://", "", url.strip().lower())
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:60].rstrip("-")
    return f"{base}-{tail}"


@router.get("/scores.json")
async def scores_json():
    """Public delivery scores — the Prober's findings (AGE-20 stage 1).

    One row per probed x402 service: delivery rate/factor over the 30d
    window, p50 latency, rail labels (MPP/USDG), flags, last-known price,
    and the human why line verified_route shows. The raw probe evidence
    stays private (tx hash + response snapshots back every negative flag);
    these scores are the public asset. No-store: reflect fresh sweeps."""
    from decimal import Decimal

    from gateway.radar import _delivery_why
    from gateway.services.supabase import (fetch_own_tool_receipts,
                                           fetch_service_scores)
    from registry.registry import get_tool, list_tools

    scores = await fetch_service_scores()
    own = await fetch_own_tool_receipts()
    for t in own:                       # enrich with the registry price
        tool = get_tool(t["tool"])
        t["price_usdc"] = tool.price_usdc if tool else None
    free_count = sum(1 for t in list_tools()
                     if Decimal(t.price_usdc or "0") == 0)
    services = []
    for url in sorted(scores, key=lambda u: (
            -(float(scores[u].get("delivery_factor") or 1.0)), u)):
        row = scores[url]
        services.append({
            "resource_url": url,
            "page": f"/s/{service_slug(url)}",
            "name": row.get("name") or url.split("//")[-1].split("/")[0],
            "need": row.get("need"),
            "network": row.get("network"),
            "window_days": row.get("window_days", 30),
            "paid_probes": row.get("paid_probes"),
            "delivery_rate": row.get("delivery_rate"),
            "delivery_factor": row.get("delivery_factor"),
            "latency_p50_ms": row.get("latency_p50_ms"),
            "flags": row.get("flags") or [],
            "mpp_option": bool(row.get("mpp_option")),
            "usdg_option": bool(row.get("usdg_option")),
            "price_usdc": row.get("price_usdc"),
            "last_ok_at": row.get("last_ok_at"),
            "last_fail_at": row.get("last_fail_at"),
            "why": _delivery_why(row),
        })
    return JSONResponse(
        {
            "about": ("AgentPay Active Prober — paid delivery-quality probes "
                      "of the x402 marketplace. Unprobed services are neutral "
                      "(factor 1.0); these scores feed verified_route ranking."),
            "count": len(services),
            "services": services,
            # AgentPay's own paid tools are code-excluded from probing (a
            # trust oracle must not score itself). Their delivery evidence is
            # real customers' receipted paid calls — verifiable on /ledger.
            "own_tools": {
                "policy": "self-excluded from probing; evidenced by customer receipts",
                "receipts_url": "https://agentpay.tools/ledger",
                "tools": own,
                # Free tools never settle on-chain, so there is no payment
                # evidence to show — surfaced as a count, not as rows that
                # could be mistaken for paid demand (AGE-38).
                "free_tools": {"count": free_count,
                               "note": "no payment needed, so no payment "
                                       "evidence to show"},
            },
        },
        headers={"Cache-Control": "no-store"},
    )


# ── Public leaderboard (the human surface for /scores.json) ──────────────────
# Self-contained HTML, same pattern/style as /radar: no build step, no external
# assets; fetches /scores.json client-side. Honest about early data — the
# prober runs Mon/Thu, so the table grows every sweep.
_PROBES_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>x402 Delivery Scores — AgentPay Prober</title>
<meta name="description" content="Live delivery scores for the x402 marketplace. The AgentPay Prober pays services real USDC twice a week and scores whether they actually deliver — latency, delivery rate, and flags, all public.">
<meta property="og:title" content="x402 Delivery Scores — AgentPay Prober">
<meta property="og:description" content="Usage stats say what's popular. Paying says what delivers. Live, public delivery scores for x402 services.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://agentpay.tools/probes">
<meta property="og:image" content="https://agentpay.tools/og.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://agentpay.tools/og.png">
<link rel="canonical" href="https://agentpay.tools/probes">
<style>
  :root{--bg:#0b0e11;--card:#13181d;--line:#222a31;--fg:#e7edf3;--mut:#8a97a6;--ac:#c3f53c;--ac2:#5ad1ff;--bad:#ff6b6b}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:980px;margin:0 auto;padding:28px 18px 60px}
  h1{font-size:24px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 18px}
  .stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
  .stat b{display:block;font-size:20px}.stat span{color:var(--mut);font-size:12px}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  td.r,th.r{text-align:right}
  .url{color:var(--mut);font-size:12px;word-break:break-all}
  .name{font-weight:600}
  .need{color:var(--mut);font-size:11px;border:1px solid var(--line);border-radius:20px;
    padding:1px 8px;margin-left:6px;vertical-align:middle}
  .why{color:var(--mut);font-size:12px;margin-top:2px}
  .badge{font-size:12px;font-weight:700;border-radius:6px;padding:2px 8px;white-space:nowrap}
  .up{color:var(--ac);border:1px solid #2c4a1f}
  .neutral{color:var(--mut);border:1px solid var(--line)}
  .down{color:var(--bad);border:1px solid #4a1f1f}
  .flag{color:var(--bad);font-size:11px;border:1px solid #4a1f1f;border-radius:6px;padding:2px 6px}
  .rail{color:var(--ac2);font-size:11px;border:1px solid #1f3a45;border-radius:6px;padding:2px 6px}
  .note{background:var(--card);border:1px solid var(--line);border-radius:10px;
    padding:10px 14px;color:var(--mut);font-size:13px;margin-bottom:18px}
  .selfbadge{font-size:12px;font-weight:700;color:var(--ac);border:1px solid #2c4a1f;
    border-radius:20px;padding:2px 10px;vertical-align:middle;margin-left:8px;white-space:nowrap}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .card .top{display:flex;justify-content:space-between;align-items:baseline}
  .card .tool{font-weight:600;font-size:14px}.card .price{color:var(--mut);font-size:12px}
  .card b{display:block;font-size:26px;margin:6px 0 0}
  .card .cap{color:var(--mut);font-size:12px}
  .card .bot{display:flex;justify-content:space-between;align-items:center;
    border-top:1px solid var(--line);margin-top:10px;padding-top:8px;font-size:12px}
  .card .when{color:var(--mut)}
  .freerow{display:flex;justify-content:space-between;align-items:center;gap:10px;
    background:var(--card);border:1px solid var(--line);border-radius:10px;
    padding:10px 14px;color:var(--mut);font-size:13px}
  .msg{color:var(--mut);padding:18px 2px}
  a{color:var(--ac2)}.foot{color:var(--mut);font-size:12px;margin-top:22px;border-top:1px solid var(--line);padding-top:14px}
</style></head><body><div class="wrap">
  <h1>x402 Delivery Scores</h1>
  <p class="sub">The AgentPay Prober pays x402 services with real USDC and scores whether
  they actually deliver. Usage stats say what's popular — paying says what works.</p>
  <div class="stats" id="stats"></div>
  <div class="note">Early data, growing fast: the Prober sweeps the marketplace every
  Monday &amp; Thursday. Unprobed services rank neutral (factor 1.00) — absence of
  data never penalizes anyone. These scores feed
  <code>verified_route</code> ranking directly.</div>
  <div id="board" class="msg">Loading scores…</div>
  <h2 style="font-size:18px;margin:28px 0 6px">AgentPay's own tools
    <span class="selfbadge">never self-scored</span></h2>
  <div class="note">A trust oracle shouldn't grade itself, so our tools are
  excluded from probing by design. The evidence below is the harder kind:
  <b>real customers paying real USDC</b> — every call verifiable on the
  <a href="/ledger">receipt ledger</a>.</div>
  <div id="own" class="msg">Loading…</div>
  <div id="ownfree"></div>
  <div class="foot">AgentPay's own tools are deliberately excluded — a trust
    oracle must not score itself (they're health-checked independently via
    x402scout). · Raw JSON: <a href="/scores.json">/scores.json</a> ·
    Methodology: paid probes over a 30-day window; delivered = payment settled ∧ HTTP 200 ∧
    non-empty response ∧ advertised schema matched. Factor: ≥90% → 1.15 boost · 50–90% → sliding ·
    &lt;50% → 0.25 · took-payment-without-delivering → flagged, never recommended.
    Negative flags are backed by on-chain tx evidence. ·
    <a href="/ledger">Receipt ledger</a> · <a href="/llms.txt">About AgentPay</a></div>
</div>
<script>
(async () => {
  const board = document.getElementById('board');
  try {
    const d = await (await fetch('/scores.json')).json();
    const svcs = d.services || [];
    const probed = svcs.filter(s => (s.paid_probes || 0) > 0);
    const flagged = svcs.filter(s => (s.flags || []).length > 0);
    const boosted = svcs.filter(s => (s.delivery_factor || 1) > 1);
    document.getElementById('stats').innerHTML = [
      ['' + svcs.length, 'services tracked'],
      ['' + probed.length, 'paid-probed (30d)'],
      ['' + boosted.length, 'proven deliverers'],
      ['' + flagged.length, 'flagged'],
    ].map(([b, s]) => `<div class="stat"><b>${b}</b><span>${s}</span></div>`).join('');
    if (!svcs.length) { board.innerHTML = '<p class="msg">No scores yet — first sweep lands Monday 05:00 UTC.</p>'; return; }
    const esc = t => String(t ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const CHAINS = {'eip155:8453':'Base','eip155:84532':'Base Sepolia',
      'eip155:1':'Ethereum','eip155:137':'Polygon','eip155:43114':'Avalanche',
      'eip155:42161':'Arbitrum','eip155:46630':'Robinhood'};
    const chain = n => {
      if (!n) return null;
      const k = String(n).toLowerCase();
      if (CHAINS[k]) return CHAINS[k];
      if (k.startsWith('solana')) return 'Solana';
      if (k.startsWith('stellar')) return 'Stellar';
      if (k.startsWith('eip155:')) return 'EVM ' + k.slice(7);
      return k.length > 14 ? k.slice(0, 12) + '…' : n;   // never a wall of hash
    };
    const badge = f => f > 1 ? `<span class="badge up">${f.toFixed(2)}×</span>`
      : f < 1 ? `<span class="badge down">${f.toFixed(2)}×</span>`
      : `<span class="badge neutral">1.00×</span>`;
    board.innerHTML = `<table><thead><tr>
      <th>Service</th><th class="r">Delivery</th><th class="r">Factor</th>
      <th class="r">p50</th><th class="r">Price</th><th>Rails / flags</th>
      </tr></thead><tbody>` + svcs.map(s => {
        const rate = s.delivery_rate == null ? '<span class="url">unprobed</span>'
          : Math.round(s.delivery_rate * 100) + '%';
        const extras = [
          chain(s.network) ? `<span class="rail">${esc(chain(s.network))}</span>` : '',
          s.mpp_option ? '<span class="rail">MPP/Tempo</span>' : '',
          s.usdg_option ? '<span class="rail">USDG</span>' : '',
          ...(s.flags || []).map(f => `<span class="flag">${esc(f)}</span>`),
        ].filter(Boolean).join(' ') || '<span class="url">—</span>';
        return `<tr><td><div><a class="name" style="color:var(--fg)" href="${esc(s.page || '#')}">${esc(s.name || '')}</a>
          ${s.need ? `<span class="need">${esc(s.need)}</span>` : ''}</div>
          <div class="url">${esc(s.resource_url)}</div>
          ${s.why ? `<div class="why">${esc(s.why)}</div>` : ''}</td>
          <td class="r">${rate}</td>
          <td class="r">${badge(Number(s.delivery_factor) || 1)}</td>
          <td class="r">${s.latency_p50_ms != null ? esc(s.latency_p50_ms) + 'ms' : '—'}</td>
          <td class="r">${s.price_usdc != null ? '$' + esc(s.price_usdc) : '—'}</td>
          <td>${extras}</td></tr>`;
      }).join('') + '</tbody></table>';
    const ownEl = document.getElementById('own');
    const own = (d.own_tools || {}).tools || [];
    const ago = iso => {
      if (!iso) return '—';
      const days = Math.floor((Date.now() - Date.parse(iso)) / 86400000);
      if (isNaN(days)) return '—';
      if (days <= 0) return 'last paid today';
      if (days === 1) return 'last paid yesterday';
      if (days < 45) return `last paid ${days}d ago`;
      return 'last paid ' + String(iso).slice(0, 10);
    };
    ownEl.className = '';
    ownEl.innerHTML = own.length
      ? '<div class="cards">' + own.map(t => `<div class="card">
          <div class="top"><span class="tool">${esc(t.tool)}</span>
            <span class="price">${t.price_usdc != null ? '$' + esc(t.price_usdc) : ''}</span></div>
          <b>${esc(t.paid_calls)}</b><span class="cap">customer-paid calls</span>
          <div class="bot"><span class="when">${esc(ago(t.last_paid_at))}</span>
            <a href="/ledger">receipts →</a></div>
        </div>`).join('') + '</div>'
      : '<p class="msg">Receipt data unavailable — see <a href="/ledger">/ledger</a>.</p>';
    const freeN = ((d.own_tools || {}).free_tools || {}).count;
    if (freeN) document.getElementById('ownfree').innerHTML =
      `<div class="freerow"><span>${esc(freeN)} free tools — no payment needed,
       so no payment evidence to show</span>
       <a href="/ledger" style="white-space:nowrap">activity on the ledger →</a></div>`;
  } catch (e) { board.innerHTML = '<p class="msg">Could not load scores — try <a href="/scores.json">/scores.json</a>.</p>'; }
})();
</script></body></html>"""


@router.get("/probes", response_class=Response)
async def probes_page():
    """Public delivery-scores leaderboard — the human surface for /scores.json."""
    return Response(content=_PROBES_HTML, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


_PAGE_CSS = """
  :root{--bg:#0b0e11;--card:#13181d;--line:#222a31;--fg:#e7edf3;--mut:#8a97a6;--ac:#c3f53c;--ac2:#5ad1ff;--bad:#ff6b6b}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:760px;margin:0 auto;padding:28px 18px 60px}
  .crumb{font-size:13px;margin:0 0 14px}.crumb a{color:var(--ac2);text-decoration:none}
  h1{font-size:22px;margin:0 0 2px;word-break:break-word}
  .url{color:var(--mut);font-size:13px;word-break:break-all;margin:0 0 14px}
  .chips{margin:0 0 16px}.chip{display:inline-block;color:var(--mut);font-size:12px;
    border:1px solid var(--line);border-radius:20px;padding:2px 10px;margin:0 6px 6px 0}
  .chip.rail{color:var(--ac2);border-color:#1f3a45}
  .chip.flag{color:var(--bad);border-color:#4a1f1f}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 16px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .stat b{display:block;font-size:22px}.stat span{color:var(--mut);font-size:12px}
  .stat b.up{color:var(--ac)}.stat b.down{color:var(--bad)}
  .why{background:var(--card);border:1px solid var(--line);border-radius:10px;
    padding:12px 14px;font-size:14px;margin:0 0 16px}
  .meta{color:var(--mut);font-size:13px;margin:0 0 20px}
  .cta{background:var(--card);border:1px solid #2c4a1f;border-radius:10px;padding:14px;font-size:14px}
  .cta a{color:var(--ac)}
  .foot{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);
    padding-top:12px}.foot a{color:var(--ac2)}
"""


@router.get("/s/{slug}", response_class=Response)
async def service_page(slug: str):
    """Per-service public page (AGE-39) — the SEO surface for one probed
    x402 service. SERVER-rendered on purpose: /probes builds its table
    client-side, which crawlers without JS see as an empty shell (the exact
    trap that kept competitors' pages ranking while ours didn't). Everything
    a search engine needs — title, description, canonical, the delivery
    evidence itself — is in the HTML we return here."""
    from gateway.radar import _delivery_why
    from gateway.services.supabase import fetch_service_scores

    scores = await fetch_service_scores()
    row, url = None, None
    for u, r in scores.items():
        if service_slug(u) == slug:
            row, url = r, u
            break
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown service")

    e = _html.escape
    name = row.get("name") or url.split("//")[-1].split("/")[0]
    why = _delivery_why(row) or ""
    rate = row.get("delivery_rate")
    rate_s = "unprobed" if rate is None else f"{round(float(rate) * 100)}%"
    factor = float(row.get("delivery_factor") or 1.0)
    lat = row.get("latency_p50_ms")
    price = row.get("price_usdc")
    probes = row.get("paid_probes") or 0
    desc = (f"{name} — x402 delivery score from the AgentPay Prober: "
            f"{rate_s} delivery over {row.get('window_days', 30)} days"
            f"{', p50 ' + str(lat) + 'ms' if lat is not None else ''}. "
            "Paid probes with real USDC, not uptime pings.")
    chips = []
    if row.get("need"):
        chips.append(f'<span class="chip">{e(str(row["need"]))}</span>')
    if row.get("network"):
        chips.append(f'<span class="chip rail">{e(str(row["network"]))}</span>')
    if row.get("mpp_option"):
        chips.append('<span class="chip rail">also payable via MPP/Tempo</span>')
    if row.get("usdg_option"):
        chips.append('<span class="chip rail">USDG</span>')
    for f in (row.get("flags") or []):
        chips.append(f'<span class="chip flag">{e(str(f))}</span>')
    fcls = "up" if factor > 1 else ("down" if factor < 1 else "")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(name)} — x402 delivery score | AgentPay Prober</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="https://agentpay.tools/s/{e(slug)}">
<meta property="og:title" content="{e(name)} — x402 delivery score">
<meta property="og:description" content="{e(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="https://agentpay.tools/s/{e(slug)}">
<style>{_PAGE_CSS}</style></head><body><div class="wrap">
<p class="crumb"><a href="/probes">← x402 delivery scores</a></p>
<h1>{e(name)}</h1>
<p class="url">{e(url)}</p>
<div class="chips">{''.join(chips)}</div>
<div class="grid">
  <div class="stat"><b>{e(rate_s)}</b><span>delivery rate ({row.get('window_days', 30)}d)</span></div>
  <div class="stat"><b class="{fcls}">{factor:.2f}×</b><span>ranking factor</span></div>
  <div class="stat"><b>{e(str(lat)) + 'ms' if lat is not None else '—'}</b><span>p50 latency</span></div>
  <div class="stat"><b>{'$' + e(str(price)) if price is not None else '—'}</b><span>price per call</span></div>
  <div class="stat"><b>{e(str(probes))}</b><span>paid probes (30d)</span></div>
</div>
{f'<div class="why">{e(why)}</div>' if why else ''}
<p class="meta">Delivered = payment settled ∧ HTTP 200 ∧ non-empty response ∧ advertised
schema matched. The AgentPay Prober pays this service real USDC on a Mon/Thu sweep —
these are settlement-verified delivery checks, not uptime pings. Negative flags are
backed by on-chain transaction evidence.</p>
<div class="cta">Routing an agent to a tool like this? <a href="/tools/verified_route">verified_route</a>
($0.01) sweeps the marketplace, applies these delivery scores, and returns one vetted,
ready-to-pay recommendation — one call instead of a score-then-choose pipeline.</div>
<div class="foot"><a href="/probes">All delivery scores</a> ·
<a href="/scores.json">Raw JSON</a> · <a href="/ledger">Receipt ledger</a> ·
<a href="/llms.txt">About AgentPay</a></div>
</div></body></html>"""
    return Response(content=page, media_type="text/html",
                    headers={"Cache-Control": "public, max-age=300"})


@router.post("/v1/prober/run")
async def prober_ingest(request: Request,
                        x_flagship_secret: str | None = Header(default=None)):
    """Ingest one prober sweep: {"probes": [...], "run": {...}?}.

    Returns what was stored plus the rebuilt window scores, so the runner's
    logs show the authoritative (history-joined) numbers, not just its own
    run. Best-effort storage: 200 when fully stored, 202 when accepted but
    not (fully) stored — the prober never fails its run over storage.
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
    if not isinstance(payload, dict) or not isinstance(payload.get("probes"), list):
        raise HTTPException(status_code=400,
                            detail='Expected {"probes": [...], "run": {...}?}')

    probes = [p for p in payload["probes"] if isinstance(p, dict)]
    probes_stored = await insert_service_probes(probes)

    # Rebuild scores over the full window. When history isn't readable
    # (table missing, blip) fall back to scoring just this run's rows —
    # better a fresh score than none.
    window = await fetch_service_probes()
    scores = score(window if window else probes)
    scores_stored = await upsert_service_scores(scores)

    run_stored = False
    run = payload.get("run")
    if isinstance(run, dict) and run:
        run_stored = await insert_flagship_run(run)

    fully = probes_stored and scores_stored and (run_stored or not run)
    return JSONResponse(
        {
            "probes_stored": probes_stored,
            "scores_stored": scores_stored,
            "run_stored": run_stored,
            "window_rows": len(window),
            "scores": scores,
        },
        status_code=200 if fully else 202,
    )
