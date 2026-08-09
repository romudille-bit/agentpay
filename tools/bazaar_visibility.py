#!/usr/bin/env python3
"""
tools/bazaar_visibility.py — AGE-111: how discoverable are we to buyer AGENTS?

Asks Coinbase Bazaar the 22 questions a buyer agent would actually ask and
reports which of AgentPay's paid listings come back. This is the measurement
that closed the AGE-36 experiment: head terms are won by keyword-in-serviceName,
not by tags or description.

Baseline 2026-08-06, before the rename (commit 8efe500): 7/22.
  hits : slippage, pre-trade check, trade safety, budget cap, spend control,
         session, agentpay
  MISS : trust, route, routing, discovery, vetting, verify delivery,
         trust oracle, delivery score, "verified route"  <- 0/9 head terms

After the rename + forced reindex, 2026-08-06: 8/22 (head terms 1/9 —
"trust oracle" started returning verified_route).

RE-MEASURED 2026-08-09: 5/22. THE NUMBER DECAYS — read it as a freshness
gauge, not a score. budget cap / spend control / session all went to zero
because session_create fell OUT of the index within three days: nothing pays
for it, and Bazaar only refreshes a record at settle time. pre_trade_check and
verified_route held only because a real customer buys them every 1-3h. If you
see a drop here, check the BRAND query first — if the brand term doesn't return
a listing, it is de-indexed rather than out-ranked, which is a different
problem with a different fix (AGE-113: agents/analyst/listing_keepalive.py).

Bazaar re-indexes from the live 402 at settle time, so re-run this only AFTER
at least one paid call per tool has settled post-deploy — otherwise you are
measuring the old record. Free, read-only, nothing settles here.

    python3 tools/bazaar_visibility.py
"""

import concurrent.futures
import json
import urllib.parse
import urllib.request

BAZAAR = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
UA = "agentpay-radar/0.1 (+https://agentpay.tools)"

# What a buyer agent asks. Split so the readout shows WHICH class we win.
HEAD_TERMS = ["trust", "route", "routing", "discovery", "vetting",
              "verify delivery", "trust oracle", "verified route",
              "delivery score"]
LONG_TAIL = ["slippage", "pre-trade check", "trade safety", "budget cap",
             "spend control", "session", "token security"]
GENERIC = ["crypto price", "market data", "api", "data",
           "which api should i pay"]
BRAND = ["agentpay"]
QUERIES = HEAD_TERMS + LONG_TAIL + GENERIC + BRAND


def fetch(q):
    url = BAZAAR + (f"?query={urllib.parse.quote(q)}" if q else "")
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:  # noqa: S310
            return q, json.loads(r.read().decode())
    except Exception as e:
        print(f"  query {q!r} failed: {e}")
        return q, None


def urls_of(payload):
    out = []
    for r in (payload or {}).get("resources", []):
        res = r.get("resource")
        u = res if isinstance(res, str) else (res or {}).get("url", "")
        if u:
            out.append((u, r.get("serviceName")
                        or (res or {}).get("serviceName") if isinstance(res, dict)
                        else r.get("serviceName")))
    return out


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = dict(ex.map(fetch, QUERIES))

    def report(label, terms):
        hits = 0
        print(f"\n── {label}")
        for q in terms:
            payload = results.get(q)
            if payload is None:
                print(f"   {q:<24} ERROR")
                continue
            found = [(u, n) for u, n in urls_of(payload)
                     if "agentpay.tools" in (u or "").lower()]
            if found:
                hits += 1
                names = ", ".join(sorted({n for _, n in found if n}))
                print(f"   {q:<24} YES ({len(found)})  {names}")
            else:
                print(f"   {q:<24} —   no   "
                      f"({len(urls_of(payload))} results, none ours)")
        print(f"   → {hits}/{len(terms)}")
        return hits

    total = 0
    total += report("HEAD TERMS (the ones name-keywords win)", HEAD_TERMS)
    total += report("LONG TAIL (tags/description are enough here)", LONG_TAIL)
    total += report("GENERIC (commodity — we deliberately don't chase)", GENERIC)
    total += report("BRAND (contested: 8 rival 'AgentPay' products)", BRAND)
    print(f"\nTOTAL: {total}/{len(QUERIES)}   (baseline before rename: 7/22)")


if __name__ == "__main__":
    main()
