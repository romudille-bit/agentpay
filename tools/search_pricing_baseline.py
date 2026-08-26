#!/usr/bin/env python3
"""
search_pricing_baseline.py — AGE-141: what does web_search / url_reader traffic
look like BEFORE (and during) the two-week $0.005 pricing test?

Two sources, because the 2026-08-20 disk-IO fix moved unpaid 402s out of
payment_logs:
  * payment_logs            — completed calls (state payment_done / verified),
                              with user_agent, agent_address, parameters
  * payment_logs_daily_rollup — 402 issuance counts (state = free_402 |
                              paid_402 | probe_get) by day / tool / user_agent

Reports, per tool, for the window:
  completions by UA bucket (own MCP, flagship/SDK, third-party), distinct
  identities, share with non-null parameters (= real intent, AGE-49 rule),
  402s by kind and top UAs, and — once the tools are priced — settles by wallet.

stdlib only (runs on the Mac venv or the Cowork VM's python3). Reads
SUPABASE_URL / SUPABASE_KEY from ../.env.

    python3 tools/search_pricing_baseline.py --days 14
    python3 tools/search_pricing_baseline.py --days 14 --json
    python3 tools/search_pricing_baseline.py --since 2026-08-27   # test window
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

TOOLS = ("web_search", "url_reader")
SELF_ADDRESSES = {
    "0xe1601c10b8d4dbf71e0c592b779520380174bc3a",  # flagship analyst
    "0xc507d39678309b2389744526a7cd86e236c6c750",  # prober wallet
    "0x1111111111111111111111111111111111111111",  # free_v2_smoke payer
}
KNOWN_PROBER_UAS = ("trustprobe", "cairn", "x402-observer", "census-probe", "carbonmonitor",
                    "mako-pulse", "fuchss", "x402-validator", "masterkey")


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


def _get(url: str, key: str, table: str, params: dict, page: int = 1000) -> list[dict]:
    rows: list[dict] = []
    off = 0
    while True:
        q = urllib.parse.urlencode(params, safe="(),.:*")
        req = urllib.request.Request(
            f"{url.rstrip('/')}/rest/v1/{table}?{q}",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Range": f"{off}-{off + page - 1}", "Range-Unit": "items"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            chunk = json.loads(r.read().decode())
        rows.extend(chunk)
        if len(chunk) < page:
            return rows
        off += page


def _bucket(ua: str | None, addr: str | None) -> str:
    u = (ua or "").lower()
    a = (addr or "").lower()
    if a in SELF_ADDRESSES or "agentpay-freev2-smoke" in u or "agentpay-loop-demo" in u:
        return "self"
    if u.startswith("agentpay-mcp"):
        return "own_mcp"
    if a.startswith("mcp-free-"):
        return "own_mcp"
    if "agentpay-x402" in u or "agentpay-sdk" in u or "agentpay/" in u:
        return "own_sdk"
    if any(k in u for k in KNOWN_PROBER_UAS):
        return "known_prober"
    if "mozilla/" in u:
        return "spoofed_browser"
    return "third_party"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--since", help="ISO date; overrides --days")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    _load_dotenv()
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not (url and key):
        sys.exit("SUPABASE_URL / SUPABASE_KEY not found in .env")

    since = (dt.date.fromisoformat(a.since) if a.since
             else dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=a.days))
    since_iso = f"{since.isoformat()}T00:00:00Z"
    tool_in = f"in.({','.join(TOOLS)})"

    logs = _get(url, key, "payment_logs", {
        "select": "created_at,tool_name,agent_address,user_agent,amount_usdc,state,network,parameters",
        "tool_name": tool_in, "created_at": f"gte.{since_iso}", "order": "created_at.desc"})
    roll = _get(url, key, "payment_logs_daily_rollup", {
        "select": "day,tool_name,user_agent,state,n",
        "tool_name": tool_in, "day": f"gte.{since.isoformat()}"})

    out: dict = {"since": since.isoformat(), "tools": {}}
    lines = [f"# web_search / url_reader baseline — since {since.isoformat()} (AGE-141)", ""]
    for tool in TOOLS:
        L = [r for r in logs if r.get("tool_name") == tool]
        R = [r for r in roll if r.get("tool_name") == tool]
        done = [r for r in L if r.get("state") in ("payment_done", "verified")]
        by_bucket = Counter(_bucket(r.get("user_agent"), r.get("agent_address")) for r in done)
        ids = defaultdict(set)
        for r in done:
            ids[_bucket(r.get("user_agent"), r.get("agent_address"))].add((r.get("agent_address") or "?").lower())
        with_params = sum(1 for r in done if r.get("parameters") not in (None, "", {}, "{}", "null"))
        paid_settles = [r for r in done if float(r.get("amount_usdc") or 0) > 0]
        paid_wallets = Counter((r.get("agent_address") or "?").lower() for r in paid_settles)
        other_states = Counter(r.get("state") for r in L if r.get("state") not in ("payment_done", "verified"))
        k402 = Counter()
        ua402 = Counter()
        for r in R:
            n = int(r.get("n") or 0)
            k402[r.get("state")] += n
            if r.get("state") in ("free_402", "paid_402"):
                ua402[(r.get("user_agent") or "(none)")[:60]] += n
        out["tools"][tool] = {
            "completions": len(done), "completions_by_bucket": dict(by_bucket),
            "distinct_identities_by_bucket": {k: len(v) for k, v in ids.items()},
            "completions_with_params": with_params,
            "paid_settles": len(paid_settles), "paid_wallets": len(paid_wallets),
            "other_states": dict(other_states),
            "rollup_402s_by_kind": dict(k402), "top_402_uas": ua402.most_common(8),
        }
        lines += [f"## {tool}", "",
                  f"- Completions: **{len(done)}** — " + ", ".join(f"{k} {v}" for k, v in by_bucket.most_common()) or "-",
                  f"- Distinct identities: " + ", ".join(f"{k} {len(v)}" for k, v in ids.items()),
                  f"- Completions with non-null parameters (intent): {with_params}/{len(done)}",
                  f"- Paid settles (amount > 0): {len(paid_settles)} from {len(paid_wallets)} wallets"
                  + (" — " + ", ".join(f"`{w[:6]}…{w[-4:]}` ×{n}" for w, n in paid_wallets.most_common(10)) if paid_wallets else ""),
                  f"- Other payment_logs states: {dict(other_states) or '-'}",
                  f"- 402s issued (rollup): " + (", ".join(f"{k} {v:,}" for k, v in k402.most_common()) or "none recorded"),
                  "- Top 402 UAs: " + ("; ".join(f"{u} ×{n:,}" for u, n in ua402.most_common(8)) or "-"),
                  ""]
    print("\n".join(lines))
    if a.json:
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
