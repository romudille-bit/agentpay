#!/usr/bin/env python3
"""
purge_prober_false_negatives.py — AGE-86 remediation.

Deletes service_probes rows whose "failure" was provably OURS, then triggers
a window rescore so /probes and /scores.json stop publishing false negatives
against named third parties.

What gets deleted (and why it is safe):

  1. Paid rows with settle_ok = false.
     Under the AGE-86 scoring model these are NEVER delivery evidence — money
     didn't verifiably move, so they say nothing about whether the seller
     delivers. Every known false negative is in this set: X (Twitter) JSON
     API's six DNS failures (no payment ever transmitted), DeepSeek's 502
     relaying the 400 WE caused with model="default", completions' pre-fix
     row. Post-AGE-86 code stores such rows with skipped/outcome labels;
     these legacy rows have no labels and only mislead.

  2. Paid rows for https://x402.shizu.me/pdf (PDF to Text) probed before
     2026-07-28. Those settled — but the pre-AGE-83 SDK dropped all params
     on GET-served resources, so we paid and then called the service with
     NO ARGUMENTS. A working service scored 0.0 three times on our bug.

Everything else — every settled probe against a fair request — is untouched.
The evidence trail for genuine failures stays intact.

Usage (dry-run by default; nothing is deleted without --execute):

    export SUPABASE_URL=https://<ref>.supabase.co
    export SUPABASE_KEY=<service key>          # NOT the anon key
    export FLAGSHIP_INGEST_SECRET=<secret>     # optional: triggers rescore
    python tools/purge_prober_false_negatives.py            # preview
    python tools/purge_prober_false_negatives.py --execute  # delete + rescore
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GATEWAY = os.environ.get("AGENTPAY_GATEWAY_URL", "https://agentpay.tools")
INGEST_SECRET = os.environ.get("FLAGSHIP_INGEST_SECRET", "")

# (label, PostgREST filter querystring)
TARGETS = [
    ("paid rows that never settled (settle_ok=false) — includes X (Twitter) "
     "JSON API's 6 DNS failures, DeepSeek's relayed-400, completions",
     "probe_type=eq.paid&settle_ok=eq.false"),
    ("PDF to Text settled rows from before the AGE-83 GET fix "
     "(paid, then called with zero arguments)",
     "probe_type=eq.paid&probed_at=lt.2026-07-28"
     "&resource_url=eq." + urllib.parse.quote(
         "https://x402.shizu.me/pdf", safe="")),
]


def _req(method: str, url: str, headers: dict, body: bytes | None = None):
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()


def _headers() -> dict:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def preview(filt: str) -> list[dict]:
    status, body = _req(
        "GET",
        f"{SUPABASE_URL}/rest/v1/service_probes?{filt}"
        "&select=resource_url,probed_at,settle_ok,error",
        _headers())
    return json.loads(body) if status == 200 else []


def delete(filt: str) -> int:
    status, body = _req(
        "DELETE", f"{SUPABASE_URL}/rest/v1/service_probes?{filt}",
        {**_headers(), "Prefer": "return=representation"})
    return len(json.loads(body)) if status in (200, 204) and body else 0


def trigger_rescore() -> bool:
    """POST an empty probe batch: the ingest endpoint refetches the full 30d
    window and rebuilds service_scores — the same path a real sweep uses."""
    if not INGEST_SECRET:
        print("  (FLAGSHIP_INGEST_SECRET unset — run a rescore manually or "
              "wait for the next sweep)")
        return False
    status, body = _req(
        "POST", f"{GATEWAY}/v1/prober/run",
        {"Content-Type": "application/json",
         "X-Flagship-Secret": INGEST_SECRET,
         "User-Agent": "Mozilla/5.0 (compatible; x402-client)"},
        json.dumps({"probes": []}).encode())
    ok = status in (200, 202)
    data = json.loads(body) if ok else {}
    print(f"  rescore {'ok' if ok else 'FAILED'} — "
          f"window_rows={data.get('window_rows')}, "
          f"scores_stored={data.get('scores_stored')}")
    return ok


def main() -> int:
    if not (SUPABASE_URL and SUPABASE_KEY):
        print("FATAL: SUPABASE_URL and SUPABASE_KEY are required")
        return 1
    execute = "--execute" in sys.argv

    print(f"\nAGE-86 purge — {'EXECUTE' if execute else 'DRY RUN (pass --execute to delete)'}\n")
    total = 0
    for label, filt in TARGETS:
        rows = preview(filt)
        print(f"── {label}")
        by_url: dict[str, int] = {}
        for r in rows:
            by_url[r["resource_url"]] = by_url.get(r["resource_url"], 0) + 1
        for url, n in sorted(by_url.items()):
            print(f"     {n:3d}  {url}")
        print(f"     {len(rows)} row(s) match")
        if execute and rows:
            deleted = delete(filt)
            print(f"     deleted {deleted}")
            total += deleted
        print()

    if execute:
        print(f"deleted {total} row(s) total; triggering window rescore…")
        trigger_rescore()
        print("\nVerify: https://agentpay.tools/scores.json — X (Twitter) JSON "
              "API, PDF to Text, DeepSeek and completions should read "
              "unprobed-neutral (rate null, factor 1.0) or carry only fair "
              "settled evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
