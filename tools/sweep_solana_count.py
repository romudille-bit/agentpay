#!/usr/bin/env python3
"""
tools/sweep_solana_count.py — AGE-104 live sweep smoke + grant-evidence counter.

Runs the exact verified_route sweep (bare query + SWEEP_QUERIES) against live
Bazaar, then prints the catalog breakdown: total scanned, per-network rail
counts, and the solana_swept number that feeds Solana grant M1 evidence.
Also shows the chain="solana" view with the probe_coverage flag.

Free — discovery only, nothing settles. Run from repo root:
    ./venv/bin/python tools/sweep_solana_count.py
"""

import concurrent.futures
import json
import urllib.parse
import urllib.request
from decimal import Decimal

from gateway import radar


def get(q):
    url = radar.BAZAAR_URL + (f"?query={urllib.parse.quote(q)}" if q else "")
    req = urllib.request.Request(
        url, headers={"User-Agent": radar.UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:  # noqa: S310
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  query {q!r} failed: {e}")
        return None


def main():
    queries = [None] + list(dict.fromkeys(radar.SWEEP_QUERIES))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        payloads = [p for p in ex.map(get, queries) if p]
    print(f"payloads: {len(payloads)}/{len(queries)} queries ok")

    out = radar.verified_route_from_payloads(
        payloads, need="ai inference", budget=Decimal("1"))
    cat = out["catalog"]
    print(f"scanned: {cat['scanned']}  after_vetting: {cat['after_vetting']}"
          f"  real_providers: {cat['real_providers']}")
    print(f"SOLANA SWEPT: {cat['solana_swept']}   <-- grant M1 evidence number")
    print("networks (per-rail):", json.dumps(cat["networks"], indent=1))

    sol = radar.verified_route_from_payloads(
        payloads, need="ai inference", budget=Decimal("1"), chain="solana")
    print(f"\nchain=solana → scanned {sol['catalog']['scanned']}, "
          f"real_providers {sol['catalog']['real_providers']}")
    rec = sol["recommendation"]
    if rec:
        print("rec:", rec["name"], "|", rec["network"],
              "|", rec.get("probe_coverage"), "| payers30d:", rec["payers30d"])
        print("ready_to_pay net:", rec["ready_to_pay"]["network"],
              "asset:", rec["ready_to_pay"]["accepts"].get("asset"))
    for s in sol["survivors"][:10]:
        print(f"  - {s['name'][:40]:40} payers:{s['payers30d']:<5} "
              f"{s.get('probe_coverage', '')}")


if __name__ == "__main__":
    main()
