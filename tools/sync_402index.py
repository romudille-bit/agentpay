#!/usr/bin/env python3
"""
sync_402index.py — Sync AgentPay's live tool registry to 402index.io.

Reads the gateway's /.well-known/l402-services (source of truth) and reconciles
it against 402index.io:
  - POSTs any tools missing from the public listing
  - DELETEs stale entries (when INDEX402_TOKEN is set — requires domain claim)

Idempotent and safe to re-run. Only submits/deletes when state has drifted.

Rate limit: 402index.io allows 10 write calls/hour/IP, so avoid force runs.

Usage:
    python3 tools/sync_402index.py [--dry-run] [--no-delete] [--force-all]

Environment:
    GATEWAY_URL      (default: https://gateway-production-2cc2.up.railway.app)
    INDEX_URL        (default: https://402index.io)
    CONTACT_EMAIL    (default: velvetvau@gmail.com)
    INDEX402_TOKEN   (optional) — domain-verification token for DELETE/PATCH.
                      When unset, stale entries are only reported.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

GATEWAY_URL   = os.environ.get("GATEWAY_URL",   "https://gateway-production-2cc2.up.railway.app")
INDEX_URL     = os.environ.get("INDEX_URL",     "https://402index.io")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "velvetvau@gmail.com")
INDEX_TOKEN   = os.environ.get("INDEX402_TOKEN", "")   # empty = read-only mode
GATEWAY_HOST  = GATEWAY_URL.split("//", 1)[-1].split("/", 1)[0]

# Map internal categories to 402index category slugs (their taxonomy is open-ended,
# we just pass a sensible value through).
CATEGORY_MAP = {
    "data":       "crypto-data",
    "defi":       "defi",
    "monitoring": "onchain-monitoring",
    "security":   "security",
    "trading":    "trading",
}


def http_get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_json(method: str, url: str, payload: dict | None, timeout: int = 15) -> tuple[int, dict]:
    """Send JSON to `url` with the given HTTP method and parse the JSON reply."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"error": str(e)}
        return e.code, err_body


def http_post_json(url: str, payload: dict, timeout: int = 15) -> tuple[int, dict]:
    return _http_json("POST", url, payload, timeout)


def http_delete_json(url: str, payload: dict, timeout: int = 15) -> tuple[int, dict]:
    return _http_json("DELETE", url, payload, timeout)


def fetch_live_tools() -> list[dict]:
    """Pull the authoritative tool list from the gateway's discovery endpoint."""
    doc = http_get_json(f"{GATEWAY_URL}/.well-known/l402-services")
    return doc.get("services", [])


def fetch_indexed_services() -> dict[str, dict]:
    """
    Return a map of {normalized_url: full_service_record} for every entry
    already indexed under this gateway's host. We key on URL (not name) so
    we catch entries the CMO may have renamed.
    """
    doc = http_get_json(f"{INDEX_URL}/api/v1/services?q={GATEWAY_HOST}&limit=200")
    items = doc.get("services") or doc.get("data") or doc.get("items") or []
    result: dict[str, dict] = {}
    for s in items:
        url = (s.get("url") or "").rstrip("/")
        if url:
            result[url] = s
    return result


def build_registration(service: dict) -> dict:
    """Shape a local tool into 402index's /api/v1/register schema."""
    tool_id   = service["id"]
    title     = service.get("name") or tool_id.replace("_", " ").title()
    # Infer category from our registry — default to 'crypto-data'.
    category  = CATEGORY_MAP.get(service.get("category", "data"), "crypto-data")
    price_usd = service.get("pricing", {}).get("amount")

    return {
        "url":             service["endpoint"],
        "name":            f"AgentPay {title}",
        "protocol":        "x402",
        "http_method":     service.get("method", "POST"),
        # The gateway returns 402 before parsing parameters, so an empty body
        # reliably triggers the payment challenge (= healthy).
        "probe_body":      json.dumps({"parameters": {}}),
        "description":     service.get("description", ""),
        "price_usd":       float(price_usd) if price_usd is not None else None,
        "payment_asset":   "USDC",
        "payment_network": "stellar-mainnet",
        "category":        category,
        "provider":        "AgentPay",
        "contact_email":   CONTACT_EMAIL,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync AgentPay tools to 402index.io")
    parser.add_argument("--dry-run",   action="store_true", help="Show diff, don't submit or delete.")
    parser.add_argument("--no-delete", action="store_true", help="Skip deletion of stale entries.")
    parser.add_argument("--force-all", action="store_true", help="Submit every tool even if already indexed.")
    args = parser.parse_args()

    print(f"Gateway: {GATEWAY_URL}")
    print(f"Index:   {INDEX_URL}")
    print(f"Mode:    {'AUTHENTICATED (add + delete)' if INDEX_TOKEN else 'read-only (add only — set INDEX402_TOKEN to enable deletes)'}")
    print()

    try:
        live = fetch_live_tools()
    except Exception as e:
        print(f"ERROR: could not fetch live tool list: {e}")
        return 1

    print(f"Live tools from gateway:        {len(live)}")
    live_urls = {s["endpoint"].rstrip("/") for s in live}

    try:
        indexed_map = fetch_indexed_services()
    except Exception as e:
        print(f"WARNING: could not fetch 402index listings ({e}) — will submit everything.")
        indexed_map = {}
    print(f"Already indexed on 402index.io: {len(indexed_map)}")
    indexed_urls = set(indexed_map.keys())

    if args.force_all:
        to_submit = live
    else:
        to_submit = [s for s in live if s["endpoint"].rstrip("/") not in indexed_urls]

    stale_urls = sorted(indexed_urls - live_urls)
    to_delete = [indexed_map[u] for u in stale_urls]

    # ── Report plan ──────────────────────────────────────────────────────────
    print()
    print(f"Tools to submit:  {len(to_submit)}")
    for s in to_submit:
        print(f"  + {s['id']:<20} {s['endpoint']}")

    print()
    print(f"Stale to delete:  {len(to_delete)}")
    for s in to_delete:
        print(f"  - {s.get('name','?'):<32} {s.get('url','?')}  (id: {s.get('id','?')})")

    if not to_submit and not to_delete:
        print("\nNothing to do — 402index.io is already up to date.")
        return 0

    if args.dry_run:
        print("\n--dry-run set, exiting without writing.")
        return 0

    # ── Apply registrations ──────────────────────────────────────────────────
    successes_add = 0
    failures: list[tuple[str, str, int, dict]] = []
    if to_submit:
        print()
        print("Registering new services...")
    for idx, svc in enumerate(to_submit, 1):
        payload = build_registration(svc)
        status, body = http_post_json(f"{INDEX_URL}/api/v1/register", payload)
        ok = 200 <= status < 300
        marker = "OK " if ok else "ERR"
        print(f"  [{idx}/{len(to_submit)}] {marker} {status} {svc['id']:<20} {body.get('message') or body.get('error') or ''}")
        if ok:
            successes_add += 1
        else:
            failures.append(("register", svc["id"], status, body))
            if status == 429:
                print("  Rate limited — stopping registrations. Re-run in an hour.")
                break
        if idx < len(to_submit):
            time.sleep(1.0)

    # ── Apply deletions (requires token) ─────────────────────────────────────
    successes_del = 0
    if to_delete:
        if args.no_delete:
            print("\n--no-delete set, skipping deletion of stale entries.")
        elif not INDEX_TOKEN:
            print()
            print("Cannot delete stale entries — INDEX402_TOKEN is not set.")
            print(f"  → Claim the domain at {INDEX_URL}/verify, save the token to your env, and re-run.")
            print(f"  → Or email hello@402index.io from {CONTACT_EMAIL}.")
        else:
            print()
            print("Deleting stale services...")
            for idx, svc in enumerate(to_delete, 1):
                sid = svc.get("id")
                if not sid:
                    print(f"  [{idx}/{len(to_delete)}] SKIP — no id on record: {svc}")
                    continue
                payload = {"domain": GATEWAY_HOST, "verification_token": INDEX_TOKEN}
                status, body = http_delete_json(f"{INDEX_URL}/api/v1/services/{sid}", payload)
                ok = 200 <= status < 300
                marker = "OK " if ok else "ERR"
                label = svc.get("name") or sid
                print(f"  [{idx}/{len(to_delete)}] {marker} {status} {label:<32} {body.get('message') or body.get('error') or ''}")
                if ok:
                    successes_del += 1
                else:
                    failures.append(("delete", str(sid), status, body))
                    if status == 429:
                        print("  Rate limited — stopping deletions. Re-run in an hour.")
                        break
                if idx < len(to_delete):
                    time.sleep(1.0)

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print(f"Registered: {successes_add}   Deleted: {successes_del}   Failed: {len(failures)}")
    if failures:
        print("Failures:")
        for op, ident, status, body in failures:
            print(f"  {op:<8} {ident:<36} status={status}  body={body}")
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
