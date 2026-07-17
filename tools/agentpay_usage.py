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
        # Added 2026-07-17. Both were landing in "real" traffic and only stayed
        # out of the KPI by accident (their UAs happen to hit the generic-runtime
        # bucket). Change either client's UA and our own spend books as revenue.
        "0xe1601C10B8d4DbF71E0c592B779520380174bc3A",  # Flagship analyst (daily cron, ~$0.02/day)
        "0x1111111111111111111111111111111111111111",  # free_v2_smoke.py test payer
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


# Automated crawlers / indexers / monitors / probes — NOT real agent users.
# Matched as substrings (case-insensitive) against the user_agent. These self-
# identify as directory indexers, uptime/trust monitors, network mappers, and
# endpoint probers: they read the 402 and leave, never pay. Left in "real
# traffic" they dwarf the handful of genuine agents and make abandonment look
# like a conversion problem when it's just discovery tooling doing its job.
CRAWLER_UA_HINTS = [
    # indexers / discovery
    "indexer", "discovery", "bazaar", "x402station", "x402scout", "scout",
    "crawler", "spider", "ari-indexer", "402explorer", "explorer", "paygent",
    # monitors / probes / mappers / verifiers (added 2026-07-11)
    "bot", "uptime", "monitor", "observer", "mapper", "network-mapper",
    "probe", "prober", "verifier", "research", "trust", "forum-labs",
    "litebeam", "dexter", "scan",
    # search-engine crawlers (added 2026-07-17) — GoogleOther hits /s/ pages and
    # tool endpoints, reads the 402, leaves. Was landing in "likely real".
    "googleother", "googlebot", "bingbot", "applebot", "duckduckbot",
]


def _is_crawler(ua):
    u = (ua or "").lower()
    return any(h in u for h in CRAWLER_UA_HINTS)


# Bare/generic HTTP runtimes with no product identity. Ambiguous — could be a
# genuine agent that never set a User-Agent, but far more often it's directory
# tooling / probes written in that runtime. Bucketed separately so they neither
# inflate "likely real" nor get asserted as crawlers. Reported as a side note.
GENERIC_RUNTIME_UA_HINTS = [
    "node", "deno", "python-httpx", "python-requests", "httpx", "aiohttp",
    "go-http-client", "okhttp", "curl", "wget", "java/", "libwww", "got (",
]


def _is_generic_runtime(ua):
    u = (ua or "").strip().lower()
    return any(h in u for h in GENERIC_RUNTIME_UA_HINTS)


# Known noise scanner — NOT a real buyer. As of 2026-06 a single `axios/1.14.0`
# client hammers the gateway (esp. the Stellar session_create path), abandons
# every 402, and never pays. Left in "real traffic" it dwarfs and skews every
# signal (volume, chain split, abandonment), so it gets its own bucket and is
# reported only as a side note. Add other confirmed-noise UAs here as found.
SCANNER_UA_HINTS = [
    "axios/1.14.0",
]

# Our own test harnesses (added 2026-07-17). free_v2_smoke.py writes real rows
# to payment_logs — incl. payment_done/verified — so without this it books its
# own smoke calls as customer traffic. Belt-and-braces with SELF_ADDRESSES:
# filter on BOTH, because genuine paid rows often carry user_agent=None and a
# UA-only filter would miss a self row whose payer we forgot to list.
SELF_TEST_UA_HINTS = [
    "agentpay-freev2-smoke",
    "agentpay-smoke",
]


def _is_self_test(ua):
    u = (ua or "").lower()
    return any(h in u for h in SELF_TEST_UA_HINTS)


# Spoofed browser UAs (added 2026-07-17). No human browses to
# /tools/{name}/call — a real browser UA on an x402 API endpoint is a scanner
# wearing a costume. These were landing in "likely real" and getting misread as
# a free-tool conversion wall (the 2026-07-17 wall-E false alarm: 6 rows from a
# stale iOS 13.2.3 UA, payer=None, abandoned). Checked AFTER _is_crawler so
# honest self-identifying bots (GoogleOther et al.) keep their own bucket.
def _is_spoofed_browser(ua):
    u = (ua or "").strip().lower()
    return u.startswith("mozilla/") and not _is_crawler(u)


def _is_scanner(ua):
    u = (ua or "").lower()
    if any(h in u for h in SCANNER_UA_HINTS):
        return True
    return _is_self_test(u) or _is_spoofed_browser(u)


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
    crawler = [r for r in rest if _is_crawler(r.get("user_agent"))]
    non_crawl = [r for r in rest if not _is_crawler(r.get("user_agent"))]
    # Bare runtimes (node/deno/httpx/…) are unattributed, not confirmed buyers —
    # keep them out of "likely real". --with-generic folds them back in.
    keep_generic = "--with-generic" in sys.argv
    generic = [] if keep_generic else [r for r in non_crawl if _is_generic_runtime(r.get("user_agent"))]
    human   = non_crawl if keep_generic else [r for r in non_crawl if not _is_generic_runtime(r.get("user_agent"))]

    print(f"\n  AgentPay usage — last {days} day(s)  (since {since_iso})")
    print(f"  {'(including our own test traffic)' if include_self else '(real traffic only — self wallets excluded)'}")
    print("  " + "─" * 58)
    print(f"  total rows              : {len(rows)}")
    print(f"  after self-filter       : {len(real)}")
    if scanner:
        n_axios  = sum(1 for r in scanner if "axios/1.14.0" in (r.get("user_agent") or "").lower())
        n_spoof  = sum(1 for r in scanner if _is_spoofed_browser(r.get("user_agent")))
        n_smoke  = sum(1 for r in scanner if _is_self_test(r.get("user_agent")))
        print(f"  └─ noise scanner        : {len(scanner)}   (abandons every 402, never pays; --with-scanner to include)")
        print(f"     ├─ axios/1.14.0      : {n_axios}")
        print(f"     ├─ spoofed browser UA: {n_spoof}   (fake iPhone/Chrome on an API endpoint — not a human)")
        print(f"     └─ our own smoke test: {n_smoke}   (free_v2_smoke.py — not traffic)")
    print(f"  └─ crawlers/indexers    : {len(crawler)}   (Bazaar/x402 directories, monitors, probes — not users)")
    if generic:
        print(f"  └─ generic runtimes     : {len(generic)}   (bare node/deno/httpx — unattributed; --with-generic to include)")
        _generic_breakdown(generic)
    print(f"  └─ likely real traffic  : {len(human)}")

    # Cohort classification (2026-07-17). A wallet that EVER appears under a
    # crawler/prober/scanner UA is one on all its rows — including rows where
    # the UA is absent. This is not pedantry: session_create settle rows carried
    # user_agent=NULL until the 2026-07-17 gateway fix, so the single row that
    # IS the KPI was the one row a UA filter could never classify. 0xEB3d1b…
    # booked as a "paying customer" on a NULL-UA session_create while its
    # pre_trade_check row read 'TrustprobeBot/1.0 (deep-probe)' — a peer trust
    # prober paying $0.01 to verify we deliver, not a buyer. Classify the WALLET.
    bot_wallets = {
        (r.get("agent_address") or "").lower()
        for r in real
        if r.get("agent_address")
        and (_is_crawler(r.get("user_agent")) or _is_scanner(r.get("user_agent")))
    }
    bot_wallets.discard("")

    def _is_bot_wallet(r):
        return (r.get("agent_address") or "").lower() in bot_wallets

    # Completed PAID sessions = the real KPI (challenge issued AND settled).
    done_sessions = [r for r in human if r.get("tool_name") == "session_create"
                     and r.get("state") in ("payment_done", "verified")
                     and not _is_bot_wallet(r)]
    bot_sessions = [r for r in human if r.get("tool_name") == "session_create"
                    and r.get("state") in ("payment_done", "verified")
                    and _is_bot_wallet(r)]
    # 'abandoned' = we issued a 402 and NOTHING came back. Exclude 'superseded'
    # (answered, settled on a tx-keyed row) and 'pending' (still in flight) —
    # counting either as abandonment overstates the wall.
    abandoned_sessions = [r for r in human if r.get("tool_name") == "session_create"
                          and r.get("state") == "abandoned"]

    agents = Counter(r.get("agent_address") for r in human if r.get("agent_address"))
    ips     = Counter(r.get("client_ip") for r in human if r.get("client_ip"))
    tools   = Counter(r.get("tool_name") for r in human if r.get("tool_name"))
    uas     = Counter(r.get("user_agent") for r in real if r.get("user_agent"))

    print(f"\n  REAL-USAGE signals (crawlers excluded):")
    print(f"    completed paid sessions : {len(done_sessions)}   <- the KPI: discovered + actually paid")
    for r in done_sessions:
        print(f"      · {(r.get('created_at') or '')[:19]}  ${r.get('amount_usdc')}  "
              f"{r.get('agent_address')}  {r.get('network','')}")
    if bot_sessions:
        print(f"    (excluded: {len(bot_sessions)} paid session(s) from prober/crawler wallets — not demand)")
        for r in bot_sessions:
            print(f"      · {(r.get('created_at') or '')[:19]}  ${r.get('amount_usdc')}  "
                  f"{r.get('agent_address')}  [bot cohort]")
    print(f"    abandoned session 402s  : {len(abandoned_sessions)}   (402 issued, NOTHING came back)")
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


PAID_TOOLS = {"session_create", "pre_trade_check", "verified_route"}


def _generic_breakdown(generic):
    """Split the generic-runtime bucket into free vs PAID tools, with completion.

    Added 2026-07-17 (AGE-49). This bucket is excluded from "likely real"
    because bare runtimes are unattributed — but collapsing it to one count
    hid a growing, zero-converting cohort on the only revenue path we have.
    A cohort that keeps hitting PAID tools and never settles is either the
    funding wall or a second scanner; either way it must be visible, not
    summarised away.
    """
    paid = [r for r in generic if r.get("tool_name") in PAID_TOOLS]
    if not paid:
        return
    done = [r for r in paid if r.get("state") in ("payment_done", "verified")]
    rate = (len(done) / len(paid) * 100) if paid else 0.0
    by_ua = Counter((r.get("user_agent") or "—").strip().lower() for r in paid)
    top = ", ".join(f"{u}×{c}" for u, c in by_ua.most_common(3))
    print(f"     ├─ on PAID tools      : {len(paid)}   ({len(done)} settled = {rate:.0f}% — see AGE-49)")
    print(f"     └─ top UAs on paid    : {top}")


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
