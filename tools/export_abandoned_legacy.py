#!/usr/bin/env python3
"""
export_abandoned_legacy.py — dump the legacy 'abandoned' payment_logs rows to
CSV before block C of db/migrations/disk_io_fix3.sql deletes them.

Disk-IO fix #3 (2026-09-01). payment_logs holds ~62.8k 'abandoned' rows —
bot 402s from before disk-IO fix #2 (2f0b03b, 08-20) — with no index on
`state`; every background scan of the table pays for them. Their only value
was the "who probes us" signal, which payment_logs_daily_rollup has carried
since 08-04. This exports the raw rows (all columns) once, so the DELETE in
the migration loses nothing.

Usage (from the repo root, on the Mac — reads SUPABASE_URL / SUPABASE_KEY
from .env; stdlib only):

    python tools/export_abandoned_legacy.py            # → notes/payment_logs_abandoned_<UTC date>.csv
    python tools/export_abandoned_legacy.py --before 2026-08-21 --out /tmp/x.csv

Read-only: the script never deletes anything. notes/ is gitignored.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_BEFORE = "2026-08-21"   # everything created before fix #2 settled


def _load_dotenv() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(os.path.dirname(here), ".env"), os.path.join(here, ".env")):
        try:
            with open(path) as fh:
                for raw in fh:
                    s = raw.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, _, v = s.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except FileNotFoundError:
            continue


def _page(url: str, key: str, params: dict, off: int, page: int) -> list[dict]:
    q = urllib.parse.urlencode(params, safe="(),.:*")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/payment_logs?{q}",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Range": f"{off}-{off + page - 1}", "Range-Unit": "items"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--before", default=DEFAULT_BEFORE,
                    help=f"export rows with created_at < this ISO date (default {DEFAULT_BEFORE})")
    ap.add_argument("--out", default=None, help="CSV path (default notes/payment_logs_abandoned_<date>.csv)")
    ap.add_argument("--page", type=int, default=1000, help="PostgREST page size (max 1000)")
    args = ap.parse_args()

    _load_dotenv()
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_KEY not set (put them in .env)", file=sys.stderr)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = args.out or os.path.join(
        root, "notes", f"payment_logs_abandoned_{dt.datetime.now(dt.timezone.utc).date().isoformat()}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    params = {"select": "*", "state": "eq.abandoned",
              "created_at": f"lt.{args.before}", "order": "id.asc"}
    n, off, writer, fh = 0, 0, None, None
    try:
        while True:
            chunk = _page(url, key, params, off, args.page)
            if not chunk:
                break
            if writer is None:
                fh = open(out, "w", newline="")
                writer = csv.DictWriter(fh, fieldnames=list(chunk[0].keys()))
                writer.writeheader()
            for row in chunk:
                writer.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                 for k, v in row.items()})
            n += len(chunk)
            off += args.page
            print(f"\r  exported {n} rows…", end="", file=sys.stderr, flush=True)
            if len(chunk) < args.page:
                break
    finally:
        if fh:
            fh.close()
    print(f"\n{n} abandoned rows (created_at < {args.before}) → {out}")
    if n == 0:
        print("nothing to export — block C has probably already run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
