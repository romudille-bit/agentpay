#!/usr/bin/env python3
"""
agentpay_usage.py — "Is anyone actually using AgentPay?"

Reads the durable payment_logs table from Supabase and reports real usage,
filtering out our own test wallets. Every AgentPay call (free or paid) writes a
payment_logs row, so this is the source of truth — unlike /stats, whose counters
are in-memory and reset on every Railway redeploy.

What counts as a REAL user: a call from an agent_address that isn't ours. The
strongest signal is a POST /v1/session/create settle (tool_name=session_create)
from an unknown address — someone discovered AgentPay (e.g. via Bazaar) and
opened a paid session.

Setup: needs SUPABASE_URL + SUPABASE_KEY in ../.env (already present for the gateway).

Usage:
  python3 tools/agentpay_usage.py            # last 7 days
  python3 tools/agentpay_usage.py 30         # last 30 days
  python3 tools/agentpay_usage.py 7 --all    # include our own test traffic too
"""

import os
import sys
import json
import urllib.request
import urllib.parse
from collections import Counter
from datetime import datetime, timezone, timedelta

# ── Our own wallets / addresses — excluded from "real user" counts ─────────────
SELF_ADDRESSES = {
    a.lower() for a in [
        "0x3312c6BE066AaEa646813365328E1893a6a2c156",  # Base test agent / index_bazaar
        "GBCVQCNFWPM3GDO4GPT4YEQ42ZHPY67QTJA3WN5ERQIKQDXKBX62SLNJ",  # Stellar test agent (mainnet)
        "GBLYTV4ZME4CARIUVG2WC4LWQUB7HQVZ5W6IZNXLYEMTUYNX2QYOUMU7",  # Stellar test agent (testnet)
        "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7",  # Base gateway wallet
        "GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",  # Stellar gateway (mainnet)
    ]
}


def _load_dotenv():
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


# Automated crawlers / indexers — NOT real agent users. Matched as substrings
# (case-insensitive) against the user_agent. Real usage = everything else.
CRAWLER_UA_HINTS = [
    "indexer", "discovery", "bazaar", "x402station", "x402scout", "bot",
    "crawler", "spider", "uptime", "ari-indexer",
]


def _is_crawler(ua):
    u = (ua or "").lower()
    return any(h in u for h in CRAWLER_UA_HINTS)


# Known noise scanner — NOT a real buyer. As of 2026-06 a single `axios/1.14.0`
# client hammers the gateway (esp. the Stellar session_create path), abandons
# every 402, and never pays. Left in "real traffic" it dwarfs and skews every
# signal (volume, chain split, abandonment), so it gets its own bucket and is
# reported only as a side note. Add other confirmed-noise UAs here as found.
SCANNER_UA_HINTS = [
    "axios/1.14.0",
]


def _is_scanner(ua):
    u = (ua or "").lower()
    return any(h in u for h in SCANNER_UA_HINTS)


def _fetch(url, key, since_iso):
    """Page through payment_logs (PostgREST caps each page at 1000)."""
    base = f"{url.rstrip('/')}/rest/v1/payment_logs"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    rows, offset, page = [], 0, 1000
    while True:
        q = urllib.parse.urlencode({
            "select": "created_at,tool_name,agent_address,client_ip,user_agent,amount_usdc,state,network",
            "created_at": f"gte.{since_iso}",
            "order": "created_at.desc",
        })
        req = urllib.request.Request(f"{base}?{q}", headers={**headers, "Range-Unit": "items",
                                                              "Range": f"{offset}-{offset+page-1}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.loads(resp.read().decode())
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
        if offset > 100000:  # safety cap
            break
    return rows


def _is_self(addr):
    return (addr or "").lower() in SELF_ADDRESSES


def main():
    _load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("✗ SUPABASE_URL / SUPABASE_KEY not found in .env"); sys.exit(1)

    days = 7
    include_self = "--all" in sys.argv
    for a in sys.argv[1:]:
        if a.isdigit():
            days = int(a)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        rows = _fetch(url, key, since_iso)
    except Exception as e:
        print(f"✗ Supabase query failed: {e}"); sys.exit(1)

    real = [r for r in rows if include_self or not _is_self(r.get("agent_address"))]
    # Split real traffic into: known noise scanner, crawlers/indexers, and the
    # remainder (likely-human/agent). Scanner is bucketed first so it never lands
    # in "real" or "crawler" counts. Pass --with-scanner to fold it back in.
    keep_scanner = "--with-scanner" in sys.argv
    scanner = [] if keep_scanner else [r for r in real if _is_scanner(r.get("user_agent"))]
    rest    = real if keep_scanner else [r for r in real if not _is_scanner(r.get("user_agent"))]
    human   = [r for r in rest if not _is_crawler(r.get("user_agent"))]
    crawler = [r for r in rest if _is_crawler(r.get("user_agent"))]

    print(f"\n  AgentPay usage — last {days} day(s)  (since {since_iso})")
    print(f"  {'(including our own test traffic)' if include_self else '(real traffic only — self wallets excluded)'}")
    print("  " + "─" * 58)
    print(f"  total rows              : {len(rows)}")
    print(f"  after self-filter       : {len(real)}")
    if scanner:
        print(f"  └─ noise scanner        : {len(scanner)}   (axios/1.14.0 — abandons every 402, never pays; --with-scanner to include)")
    print(f"  └─ crawlers/indexers    : {len(crawler)}   (Bazaar/x402 directories probing — not users)")
    print(f"  └─ likely real traffic  : {len(human)}")

    # Completed PAID sessions = the real KPI (challenge issued AND settled).
    done_sessions = [r for r in human if r.get("tool_name") == "session_create"
                     and r.get("state") in ("payment_done", "verified")]
    abandoned_sessions = [r for r in human if r.get("tool_name") == "session_create"
                          and r.get("state") not in ("payment_done", "verified")]

    agents = Counter(r.get("agent_address") for r in human if r.get("agent_address"))
    ips     = Counter(r.get("client_ip") for r in human if r.get("client_ip"))
    tools   = Counter(r.get("tool_name") for r in human if r.get("tool_name"))
    uas     = Counter(r.get("user_agent") for r in real if r.get("user_agent"))

    print(f"\n  REAL-USAGE signals (crawlers excluded):")
    print(f"    completed paid sessions : {len(done_sessions)}   <- the KPI: discovered + actually paid")
    print(f"    abandoned session 402s  : {len(abandoned_sessions)}   (got the challenge, didn't pay)")
    print(f"    unique IPs              : {len(ips)}")
    print(f"    unique agent wallets    : {len(agents)}   (note: quickstart mints a NEW wallet per run,")
    print(f"                                       so this overcounts distinct users)")

    if not human:
        print("\n  No non-crawler activity yet. Bazaar's crawler is hitting you (good), but no")
        print("  real agent has called through yet. Run with --all to confirm logging works.\n")
        _top_simple(uas, "user-agents seen (incl. crawlers)")
        return

    def _top(counter, label, n=8):
        if not counter:
            return
        print(f"\n  Top {label}:")
        for k, c in counter.most_common(n):
            disp = (k[:46] + "…") if k and len(k) > 47 else k
            print(f"    {c:>4}  {disp}")

    _top(tools, "tools called (real traffic)")
    _top(uas, "user-agents (all non-self, incl. crawlers)")

    print("\n  Most recent likely-real calls:")
    for r in human[:12]:
        ts = (r.get("created_at") or "")[:19]
        ag = (r.get("agent_address") or "—")[:14]
        print(f"    {ts}  {r.get('tool_name','?'):<16} {r.get('state','?'):<12} ${r.get('amount_usdc','0')}  {ag}  {r.get('network','')}")
    print()


def _top_simple(counter, label, n=10):
    if not counter:
        return
    print(f"\n  {label}:")
    for k, c in counter.most_common(n):
        disp = (k[:50] + "…") if k and len(k) > 51 else k
        print(f"    {c:>4}  {disp}")


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    main()
