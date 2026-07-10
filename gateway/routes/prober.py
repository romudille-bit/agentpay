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
from fastapi.responses import JSONResponse

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
