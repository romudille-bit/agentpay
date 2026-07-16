#!/usr/bin/env python3
"""
tools/submit_indexnow.py — submit every sitemap URL to IndexNow.

IndexNow (indexnow.org) gives instant indexing on Bing, Seznam, Naver, and
Yandex (Google doesn't participate — that's what Search Console is for).
One batch POST to any participating endpoint propagates to all of them.

Prerequisites:
  1. INDEXNOW_KEY set in Railway (gateway serves it at GET /indexnow.txt)
  2. The key file must be live BEFORE submitting — the engines fetch
     keyLocation to prove domain ownership, and a failed fetch quietly
     voids the submission.

Usage:
    python3 tools/submit_indexnow.py            # submit all sitemap URLs
    python3 tools/submit_indexnow.py --dry-run  # show what would be sent

Re-run after any deploy that adds/changes public pages (new tools, new /s/
pages). Safe to re-submit: engines dedupe, but don't spam it on a cron —
submit on content change, which is the protocol's contract.
"""

import argparse
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET

GATEWAY = "https://agentpay.tools"
ENDPOINT = "https://api.indexnow.org/indexnow"   # propagates to all engines
UA = "AgentPay-IndexNow/1.0 (+https://agentpay.tools)"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 1. The served key IS the source of truth (must match what engines see).
    try:
        key = fetch(f"{GATEWAY}/indexnow.txt").decode().strip()
    except Exception as e:
        print(f"✗ {GATEWAY}/indexnow.txt not live ({e}).\n"
              "  Set INDEXNOW_KEY in Railway and deploy first — submitting "
              "before the key file is live voids the submission.")
        return 1
    if not key:
        print("✗ /indexnow.txt is empty")
        return 1
    print(f"✓ key file live ({key[:6]}…)")

    # 2. Collect URLs from the live sitemap.
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(fetch(f"{GATEWAY}/sitemap.xml"))
    urls = [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]
    print(f"✓ sitemap: {len(urls)} URLs")

    payload = {
        "host": "agentpay.tools",
        "key": key,
        "keyLocation": f"{GATEWAY}/indexnow.txt",
        "urlList": urls,          # batch limit is 10,000 — we're fine
    }
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    # 3. One batch POST.
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"✓ submitted {len(urls)} URLs — HTTP {r.status} "
                  "(200/202 = accepted; results appear in Bing Webmaster Tools)")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        print(f"✗ HTTP {e.code}: {body}\n"
              "  403 = key mismatch/keyLocation unreachable · "
              "422 = URL/host mismatch · 429 = slow down")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
