#!/usr/bin/env python3
"""
census_multi_provider.py — AGE-140: how many x402 payer wallets on Base pay
≥3 distinct providers, and what share of the dollars do they carry?

Why
---
The 2026-08-24 strategy note's thesis ("an agent paying BlockRun + Exa +
StableEnrich holds three ledgers and no total — we are the total") only has a
market if wallets that pay MORE THAN ONE provider exist in meaningful number and
carry meaningful dollars. The payer-sybil finding says 96.5% of x402 payers only
ever pay ONE recipient. This script measures the other 3.5%: count, dollar
share, and whether they look like buyers (deep, repeated spend per provider) or
probers (one payment to each of many providers — the trust layer testing
sellers, not customers).

Denominator (always stated in the report)
-----------------------------------------
USDC transfers on Base in the last N days settled through an EIP-3009
authorization (the USDC contract emits AuthorizationUsed on every
transferWithAuthorization / receiveWithAuthorization). That is the x402 "exact"
scheme fingerprint on EVM — independent of facilitator, and needing no
payTo/catalog list, so off-catalog providers are included. Not exclusively x402
(some wallets use gasless USDC sends); the relayer distribution in the report
shows how much of the set is submitted by known x402 facilitators.

Data source
-----------
Two saved Dune queries (SQL in tools/sql/):
  * census_payers.sql  → one row per payer  (legs, usd, recipients, top relayer)
  * census_pairs.sql   → one row per (payer, recipient) for multi-recipient payers
Create each once in the Dune UI (paste the SQL, add the parameters), note the
query ids, then run:

    source venv/bin/activate
    python3 tools/census_multi_provider.py --payers-query-id 123456 --pairs-query-id 123457

DUNE_API_KEY is read from ../.env (already present for the dune_query tool).
Or export the two result sets as CSV from the Dune UI and pass
--payers-csv / --pairs-csv instead (no API credits used).

Provider resolution (optional): if SUPABASE_URL/SUPABASE_KEY are in .env the
script pulls the distinct pay_to set from service_probes and reports what share
of the ≥3-recipient buyers' recipients (by wallet and by dollar) we can already
name. That number is the baseline for AGE-138's weekly map-coverage metric.

Output
------
reviews/CENSUS_MULTI_PROVIDER_<date>.md (untracked dir) + a headline on stdout.
Addresses are truncated in the report by default (data-sharing boundary);
--full-addresses keeps them whole for internal use.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from typing import Iterable

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

DUNE_API_BASE = "https://api.dune.com/api/v1"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Our own wallets — excluded from the payer population, reported separately.
SELF_PAYERS = {
    "0xe1601c10b8d4dbf71e0c592b779520380174bc3a",  # flagship analyst (daily cron)
    "0xc507d39678309b2389744526a7cd86e236c6c750",  # prober wallet (Mon/Thu sweeps)
}
OUR_PAYTO = "0xe8b25a72dd6aef69515452a61ad231c7df2843b7"  # AgentPay gateway on Base

# Facilitator relayers we can name. Extend as x402scan /facilitators evolves.
# Values are labels; keys are lowercase tx-sender addresses. Unknown relayers are
# reported by address so they can be added here.
KNOWN_RELAYERS: dict[str, str] = {
    # CDP (Coinbase) facilitator relayer — prefix seen on every gasless settle to us
    # (full address is filled in by the first run: see "unknown relayers" section).
}

BUCKETS = [(1, 1, "1"), (2, 2, "2"), (3, 5, "3–5"), (6, 20, "6–20"), (21, 10**9, "21+")]
PROBER_FANOUT_MAX = 1.5      # legs ÷ distinct recipients at or below this = prober-shaped
HEAVY_LEGS = 50              # "heavy buyer" threshold inside the ≥3-recipient set


# ── env ──────────────────────────────────────────────────────────────────────

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


# ── Dune ─────────────────────────────────────────────────────────────────────

def dune_run(query_id: int, params: dict, api_key: str, use_latest: bool = False,
             page: int = 30000, timeout_s: int = 900) -> list[dict]:
    """Execute a saved query (or fetch its latest result) and return all rows."""
    if httpx is None:
        sys.exit("httpx missing — run from the repo venv")
    h = {"X-DUNE-API-KEY": api_key}
    with httpx.Client(timeout=120.0) as c:
        if use_latest:
            base = f"{DUNE_API_BASE}/query/{query_id}/results"
        else:
            body: dict = {"performance": "medium"}
            if params:
                body["query_parameters"] = params
            r = c.post(f"{DUNE_API_BASE}/query/{query_id}/execute", headers=h, json=body)
            r.raise_for_status()
            ex = r.json()["execution_id"]
            t0 = time.time()
            while True:
                s = c.get(f"{DUNE_API_BASE}/execution/{ex}/status", headers=h).json()
                st = s.get("state")
                if st == "QUERY_STATE_COMPLETED":
                    break
                if st in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
                    sys.exit(f"Dune execution {ex} ended in {st}: {s.get('error')}")
                if time.time() - t0 > timeout_s:
                    sys.exit(f"Dune execution {ex} still {st} after {timeout_s}s")
                print(f"  … {st}", file=sys.stderr)
                time.sleep(5)
            base = f"{DUNE_API_BASE}/execution/{ex}/results"
        rows: list[dict] = []
        offset = 0
        while True:
            r = c.get(base, headers=h, params={"limit": page, "offset": offset})
            r.raise_for_status()
            j = r.json()
            chunk = j.get("result", {}).get("rows", [])
            rows.extend(chunk)
            meta = j.get("result", {}).get("metadata", {})
            total = meta.get("total_row_count")
            offset += len(chunk)
            if not chunk or len(chunk) < page or (total is not None and offset >= total):
                break
        return rows


def read_csv(path: str) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


# ── analysis ─────────────────────────────────────────────────────────────────

def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _i(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _lower(a) -> str:
    return (a or "").lower()


def _short(a: str, full: bool) -> str:
    a = a or ""
    return a if full or len(a) < 12 else f"{a[:6]}…{a[-4:]}"


def _median(xs: Iterable[float]) -> float:
    xs = list(xs)
    return statistics.median(xs) if xs else 0.0


def _bucket(n: int) -> str:
    for lo, hi, label in BUCKETS:
        if lo <= n <= hi:
            return label
    return "21+"


def fetch_known_paytos() -> set[str]:
    """Distinct pay_to from service_probes (public SELECT) + our own gateway."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    known = {OUR_PAYTO}
    if not (url and key and httpx):
        return known
    try:
        h = {"apikey": key, "Authorization": f"Bearer {key}"}
        off = 0
        with httpx.Client(timeout=60.0) as c:
            while True:
                r = c.get(f"{url.rstrip('/')}/rest/v1/service_probes",
                          headers={**h, "Range": f"{off}-{off + 999}"},
                          params={"select": "pay_to", "pay_to": "not.is.null"})
                if r.status_code not in (200, 206):
                    break
                rows = r.json()
                known.update(_lower(x.get("pay_to")) for x in rows if x.get("pay_to"))
                if len(rows) < 1000:
                    break
                off += 1000
    except Exception as e:  # best-effort
        print(f"  (service_probes lookup skipped: {e})", file=sys.stderr)
    return known


def analyse(payers: list[dict], pairs: list[dict], days: int, full: bool) -> tuple[str, dict]:
    # normalise
    P = []
    self_rows = []
    for r in payers:
        row = {
            "payer": _lower(r.get("payer")),
            "legs": _i(r.get("legs")),
            "usd": _f(r.get("usd")),
            "recipients": _i(r.get("recipients")),
            "relayers": _i(r.get("relayers")),
            "top_relayer": _lower(r.get("top_relayer")),
            "first_leg": r.get("first_leg"),
            "last_leg": r.get("last_leg"),
        }
        (self_rows if row["payer"] in SELF_PAYERS else P).append(row)

    total_w = len(P)
    total_usd = sum(r["usd"] for r in P)
    total_legs = sum(r["legs"] for r in P)

    # buckets
    by_b: dict[str, list[dict]] = defaultdict(list)
    for r in P:
        by_b[_bucket(r["recipients"])].append(r)

    # ≥3 population
    multi = [r for r in P if r["recipients"] >= 3]
    probers = [r for r in multi if r["legs"] / max(r["recipients"], 1) <= PROBER_FANOUT_MAX]
    buyers = [r for r in multi if r not in probers]
    heavy = [r for r in buyers if r["legs"] >= HEAVY_LEGS]

    # relayer distribution (approx: each payer's legs attributed to its top relayer)
    relayer_legs: Counter = Counter()
    for r in P:
        relayer_legs[r["top_relayer"]] += r["legs"]

    # pairs → per-payer recipient lists (only for multi-recipient payers)
    rec_by_payer: dict[str, list[dict]] = defaultdict(list)
    for pr in pairs:
        p = _lower(pr.get("payer"))
        if p in SELF_PAYERS:
            continue
        rec_by_payer[p].append({
            "recipient": _lower(pr.get("recipient")),
            "legs": _i(pr.get("legs")),
            "usd": _f(pr.get("usd")),
        })

    # provider resolution on the buyers' recipients
    known = fetch_known_paytos()
    buyer_set = {r["payer"] for r in buyers}
    rec_usd: Counter = Counter()
    rec_wallets: set[str] = set()
    for p, recs in rec_by_payer.items():
        if p not in buyer_set:
            continue
        for x in recs:
            rec_usd[x["recipient"]] += x["usd"]
            rec_wallets.add(x["recipient"])
    resolved_w = sum(1 for a in rec_wallets if a in known)
    resolved_usd = sum(u for a, u in rec_usd.items() if a in known)
    buyers_rec_usd = sum(rec_usd.values())

    # our own position
    our_payers = [p for p, recs in rec_by_payer.items() if any(x["recipient"] == OUR_PAYTO for x in recs)]
    our_from_multi = [p for p in our_payers if p in buyer_set]

    # ── report ───────────────────────────────────────────────────────────────
    today = dt.date.today().isoformat()
    L: list[str] = []
    L.append(f"# Multi-provider buyer census — Base, last {days} days ({today})")
    L.append("")
    L.append("**AGE-140 · internal · reviews/ is untracked — do not commit. Addresses "
             + ("shown in full." if full else "truncated; run with --full-addresses for internal use.") + "**")
    L.append("")
    L.append("## Denominator")
    L.append("")
    L.append(f"USDC transfers on Base in the last {days} days settled through an **EIP-3009 "
             "authorization** (`AuthorizationUsed` emitted by the USDC contract on every "
             "`transferWithAuthorization` / `receiveWithAuthorization`). This is the x402 "
             "\"exact\"-scheme fingerprint on EVM: facilitator-independent, no payTo or catalog "
             "list, so off-catalog providers are included. It is not exclusively x402 — some "
             "wallets use gasless USDC sends — see the relayer table for how much of the set "
             "is submitted by known x402 facilitators. Solana is excluded (different plumbing). "
             "Our own flagship and prober wallets are excluded from the population and reported "
             "separately. **Wallet ≠ operator**: one operator rotating wallets reads as N "
             "single-recipient wallets; fanout shape partially corrects for that, the funding "
             "graph (AGE-133) would fully correct.")
    L.append("")
    L.append("## Headline")
    L.append("")
    pct = lambda a, b: (100.0 * a / b) if b else 0.0
    m_usd = sum(r["usd"] for r in multi)
    b_usd = sum(r["usd"] for r in buyers)
    p_usd = sum(r["usd"] for r in probers)
    h_usd = sum(r["usd"] for r in heavy)
    L.append(f"- **{total_w:,} payer wallets** sent **${total_usd:,.2f}** over **{total_legs:,} settlements**.")
    L.append(f"- **{len(multi):,} wallets ({pct(len(multi), total_w):.1f}%) paid ≥3 distinct providers**; "
             f"they sent **${m_usd:,.2f} ({pct(m_usd, total_usd):.1f}% of dollars)**.")
    L.append(f"- Inside that set: **{len(buyers):,} look like buyers** (legs ÷ recipients > {PROBER_FANOUT_MAX}) "
             f"carrying **${b_usd:,.2f} ({pct(b_usd, total_usd):.1f}%)**; "
             f"**{len(probers):,} look like probers** (≈1 payment per recipient) carrying ${p_usd:,.2f} "
             f"({pct(p_usd, total_usd):.1f}%).")
    L.append(f"- **{len(heavy):,} heavy multi-provider buyers** (≥{HEAVY_LEGS} settlements, ≥3 providers) "
             f"carry **${h_usd:,.2f} ({pct(h_usd, total_usd):.1f}%)**.")
    if rec_wallets:
        L.append(f"- Provider resolution on the buyers' recipients: **{resolved_w}/{len(rec_wallets)} wallets "
                 f"({pct(resolved_w, len(rec_wallets)):.0f}%)** and **{pct(resolved_usd, buyers_rec_usd):.0f}% of their dollars** "
                 f"resolve to a payTo we already hold in `service_probes` (+ our own gateway). "
                 "This is the AGE-138 map-coverage baseline.")
    L.append("")
    verdict = (f"{len(multi):,} wallets pay ≥3 providers; they send {pct(m_usd, total_usd):.1f}% of "
               f"EIP-3009-settled Base USDC; of those, {len(buyers):,} look like buyers (fanout ≫1, "
               f"{pct(b_usd, total_usd):.1f}% of dollars) and {len(probers):,} like probers.")
    L.append(f"**Verdict line:** {verdict}")
    L.append("")
    L.append("## By distinct recipients")
    L.append("")
    L.append("| Recipients | Wallets | Wallet % | USD | USD % | Median legs | Median $/leg | Median legs/recipient |")
    L.append("|---|---|---|---|---|---|---|---|")
    for _, _, label in BUCKETS:
        rows = by_b.get(label, [])
        u = sum(r["usd"] for r in rows)
        L.append(f"| {label} | {len(rows):,} | {pct(len(rows), total_w):.1f}% | ${u:,.2f} | {pct(u, total_usd):.1f}% | "
                 f"{_median(r['legs'] for r in rows):.0f} | "
                 f"${_median((r['usd'] / r['legs']) for r in rows if r['legs']):.4f} | "
                 f"{_median((r['legs'] / max(r['recipients'], 1)) for r in rows):.1f} |")
    L.append("")
    L.append("## Relayers (tx senders) — is this set x402?")
    L.append("")
    L.append("Approximation: each payer's settlements attributed to its most-used relayer.")
    L.append("")
    L.append("| Relayer | Label | Legs (approx) | Share |")
    L.append("|---|---|---|---|")
    for addr, n in relayer_legs.most_common(12):
        L.append(f"| `{_short(addr, full)}` | {KNOWN_RELAYERS.get(addr, '(unknown — add to KNOWN_RELAYERS)')} | {n:,} | {pct(n, total_legs):.1f}% |")
    L.append("")
    L.append(f"## Top multi-provider buyers (≥3 providers, fanout > {PROBER_FANOUT_MAX})")
    L.append("")
    L.append("| Payer | Legs | USD | Providers | Legs/provider | Top providers (usd) |")
    L.append("|---|---|---|---|---|---|")
    for r in sorted(buyers, key=lambda x: -x["usd"])[:25]:
        recs = sorted(rec_by_payer.get(r["payer"], []), key=lambda x: -x["usd"])[:3]
        tops = ", ".join(f"`{_short(x['recipient'], full)}` ${x['usd']:.2f}" + (" **(us)**" if x["recipient"] == OUR_PAYTO else "") for x in recs) or "(pairs query not supplied)"
        L.append(f"| `{_short(r['payer'], full)}` | {r['legs']:,} | ${r['usd']:,.2f} | {r['recipients']} | "
                 f"{r['legs'] / max(r['recipients'], 1):.1f} | {tops} |")
    L.append("")
    L.append("## Prober-shaped multi-provider wallets (top 10 by providers)")
    L.append("")
    L.append("| Payer | Legs | USD | Providers | Legs/provider |")
    L.append("|---|---|---|---|---|")
    for r in sorted(probers, key=lambda x: -x["recipients"])[:10]:
        L.append(f"| `{_short(r['payer'], full)}` | {r['legs']:,} | ${r['usd']:,.2f} | {r['recipients']} | "
                 f"{r['legs'] / max(r['recipients'], 1):.2f} |")
    L.append("")
    L.append("## Our position")
    L.append("")
    if rec_by_payer:
        L.append(f"- {len(our_payers)} payer wallets in the multi-recipient set paid our gateway; "
                 f"{len(our_from_multi)} of them are multi-provider *buyers* (the population the thesis targets).")
    else:
        L.append("- (pairs query not supplied — cannot place our gateway among the buyers' recipients)")
    for r in self_rows:
        L.append(f"- Own wallet `{_short(r['payer'], full)}`: {r['legs']} legs / ${r['usd']:.2f} / {r['recipients']} providers (excluded from population).")
    L.append("")
    L.append("## Reading the result (decision this feeds — AGE-140)")
    L.append("")
    L.append("- If multi-provider **buyers** number in the hundreds and carry a majority of dollars, the "
             "ledger/\"total\" thesis is a real, small, **named** market: go-to-market is the list above, not a funnel.")
    L.append("- If they carry a small share, the ledger is a wallet feature; keep receipts inside the July "
             "session spec and spend the effort on AGE-138 / verified_route.")
    L.append("- Either way the provider-resolution % is the number to move (AGE-138).")
    L.append("")
    L.append("## Caveats")
    L.append("")
    L.append("- Base only; no Solana. Under-counts Ramp/Solana-driven multi-provider agents.")
    L.append("- EIP-3009 filter includes non-x402 gasless USDC sends; check the relayer table. It also misses "
             "direct (self-submitted, non-authorized) x402 settlements — a coverage gap, not a bias toward the thesis.")
    L.append("- Probers that pay twice (replay checks) can look like buyers on 2 legs; the fanout cut is a "
             f"shape heuristic (≤{PROBER_FANOUT_MAX} legs/recipient), not proof.")
    L.append("- Internal only. Public copy gets the shape (percentages, counts), never the wallet list.")
    L.append("")
    L.append(f"*Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by tools/census_multi_provider.py; "
             f"sources: Dune (tools/sql/census_payers.sql, census_pairs.sql), service_probes (payTo set).*")

    summary = {
        "days": days, "wallets": total_w, "usd": round(total_usd, 2), "legs": total_legs,
        "multi_wallets": len(multi), "multi_usd_pct": round(pct(m_usd, total_usd), 1),
        "buyers": len(buyers), "buyers_usd_pct": round(pct(b_usd, total_usd), 1),
        "probers": len(probers), "heavy_buyers": len(heavy), "heavy_usd_pct": round(pct(h_usd, total_usd), 1),
        "resolved_wallet_pct": round(pct(resolved_w, len(rec_wallets)), 1) if rec_wallets else None,
        "resolved_usd_pct": round(pct(resolved_usd, buyers_rec_usd), 1) if buyers_rec_usd else None,
        "verdict": verdict,
    }
    return "\n".join(L) + "\n", summary


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payers-query-id", type=int, help="Dune query id for tools/sql/census_payers.sql")
    ap.add_argument("--pairs-query-id", type=int, help="Dune query id for tools/sql/census_pairs.sql (optional)")
    ap.add_argument("--payers-csv", help="CSV export of the payers query (alternative to Dune API)")
    ap.add_argument("--pairs-csv", help="CSV export of the pairs query (alternative to Dune API)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-recipients", type=int, default=2, help="pairs query parameter")
    ap.add_argument("--use-latest", action="store_true", help="fetch the query's latest cached result instead of executing (no credits)")
    ap.add_argument("--no-params", action="store_true",
                    help="saved queries have the window hardcoded (no {{days}}/{{min_recipients}} parameters) — send none")
    ap.add_argument("--full-addresses", action="store_true")
    ap.add_argument("--out", help="report path (default reviews/CENSUS_MULTI_PROVIDER_<date>.md)")
    ap.add_argument("--json", action="store_true", help="also print the summary as JSON")
    a = ap.parse_args()

    _load_dotenv()
    payers: list[dict] = []
    pairs: list[dict] = []

    if a.payers_csv:
        payers = read_csv(a.payers_csv)
    elif a.payers_query_id:
        key = os.environ.get("DUNE_API_KEY")
        if not key:
            sys.exit("DUNE_API_KEY not found in .env")
        print(f"→ Dune payers query {a.payers_query_id} (days={a.days})", file=sys.stderr)
        payers = dune_run(a.payers_query_id, {} if a.no_params else {"days": a.days}, key, a.use_latest)
    else:
        sys.exit("need --payers-query-id or --payers-csv")

    if a.pairs_csv:
        pairs = read_csv(a.pairs_csv)
    elif a.pairs_query_id:
        key = os.environ.get("DUNE_API_KEY")
        print(f"→ Dune pairs query {a.pairs_query_id} (days={a.days}, min_recipients={a.min_recipients})", file=sys.stderr)
        pairs = dune_run(a.pairs_query_id,
                         {} if a.no_params else {"days": a.days, "min_recipients": a.min_recipients},
                         key, a.use_latest)

    print(f"  payers rows: {len(payers):,}   pairs rows: {len(pairs):,}", file=sys.stderr)
    report, summary = analyse(payers, pairs, a.days, a.full_addresses)

    here = os.path.dirname(os.path.abspath(__file__))
    out = a.out or os.path.join(os.path.dirname(here), "reviews",
                                f"CENSUS_MULTI_PROVIDER_{dt.date.today().isoformat()}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(report)
    print(f"✓ wrote {out}")
    print(summary["verdict"])
    if a.json:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
