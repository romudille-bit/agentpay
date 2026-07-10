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

import hmac
import logging

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


@router.get("/scores.json")
async def scores_json():
    """Public delivery scores — the Prober's findings (AGE-20 stage 1).

    One row per probed x402 service: delivery rate/factor over the 30d
    window, p50 latency, rail labels (MPP/USDG), flags, last-known price,
    and the human why line verified_route shows. The raw probe evidence
    stays private (tx hash + response snapshots back every negative flag);
    these scores are the public asset. No-store: reflect fresh sweeps."""
    from gateway.radar import _delivery_why
    from gateway.services.supabase import fetch_service_scores

    scores = await fetch_service_scores()
    services = []
    for url in sorted(scores, key=lambda u: (
            -(float(scores[u].get("delivery_factor") or 1.0)), u)):
        row = scores[url]
        services.append({
            "resource_url": url,
            "name": row.get("name") or url.split("//")[-1].split("/")[0],
            "need": row.get("need"),
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
  <div class="foot">Raw JSON: <a href="/scores.json">/scores.json</a> ·
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
          s.mpp_option ? '<span class="rail">MPP/Tempo</span>' : '',
          s.usdg_option ? '<span class="rail">USDG</span>' : '',
          ...(s.flags || []).map(f => `<span class="flag">${esc(f)}</span>`),
        ].filter(Boolean).join(' ') || '<span class="url">—</span>';
        return `<tr><td><div><span class="name">${esc(s.name || '')}</span>
          ${s.need ? `<span class="need">${esc(s.need)}</span>` : ''}</div>
          <div class="url">${esc(s.resource_url)}</div>
          ${s.why ? `<div class="why">${esc(s.why)}</div>` : ''}</td>
          <td class="r">${rate}</td>
          <td class="r">${badge(Number(s.delivery_factor) || 1)}</td>
          <td class="r">${s.latency_p50_ms != null ? esc(s.latency_p50_ms) + 'ms' : '—'}</td>
          <td class="r">${s.price_usdc != null ? '$' + esc(s.price_usdc) : '—'}</td>
          <td>${extras}</td></tr>`;
      }).join('') + '</tbody></table>';
  } catch (e) { board.innerHTML = '<p class="msg">Could not load scores — try <a href="/scores.json">/scores.json</a>.</p>'; }
})();
</script></body></html>"""


@router.get("/probes", response_class=Response)
async def probes_page():
    """Public delivery-scores leaderboard — the human surface for /scores.json."""
    return Response(content=_PROBES_HTML, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


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
