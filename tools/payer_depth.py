#!/usr/bin/env python3
"""
payer_depth.py — AGE-138 repeat-depth ranking (AGE-133 phase 0): pull the
per-seller payer shape from Dune (tools/sql/payer_depth_x402.sql), write the
calibration report, and (optionally) upsert `provider_depth` in Supabase so
verified_route can weight payers by whether they came back.

Why a laptop tool and not a gateway job: the extraction is ONE Dune query over
the whole x402 market (≈1–2k sellers), re-run for free from the Dune UI and
fetched with --use-latest; a per-payTo Blockscout crawl from the gateway would
be ~300 calls/night for a strict subset of the same numbers. The gateway only
READS provider_depth (cached, 7-day staleness fallback in radar.decide()).

    source venv/bin/activate
    python3 tools/payer_depth.py --query-id <digits> --use-latest            # report only
    python3 tools/payer_depth.py --query-id <digits> --use-latest --write    # + upsert provider_depth
    python3 tools/payer_depth.py --csv ~/Downloads/payer_depth.csv --write   # from a Dune CSV export

DUNE_API_KEY / SUPABASE_URL / SUPABASE_KEY are read from ../.env. Aggregates
only leave Dune — no payer wallet is ever written to Supabase or the report.
Report addresses are shortened unless --full-addresses (data-sharing rule).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from census_multi_provider import (_f, _i, _load_dotenv, _lower,  # noqa: E402
                                   dune_run, fetch_known_paytos, read_csv)

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

OUR_PAYTO = "0xe8b25a72dd6aef69515452a61ad231c7df2843b7"   # AgentPay gateway (Base)
NETWORK = "eip155:8453"

# Calibration controls (AGE-133): host substrings → label. Cluster is the
# known-synthetic positive; ApiToll / Otto are credible organic. Extra ones via
# --control label=0xaddress. Resolution is by Bazaar host when we can sweep.
CONTROL_HOSTS = {
    "apitoll": "ApiToll (organic)",
    "ottoai": "Otto (organic)",
    "cluster": "Cluster (synthetic)",
    "blockrun": "BlockRun",
    "stableenrich": "StableEnrich",
}


def _short(a: str, full: bool) -> str:
    return a if full or len(a) < 12 else f"{a[:6]}…{a[-4:]}"


def _ts(x) -> str:
    return str(x or "")[:19].replace(" ", "T")


# ── name resolution (best-effort, all public) ────────────────────────────────

def bazaar_hosts() -> dict[str, tuple[str, str]]:
    """{pay_to: (host, name)} from a live Bazaar sweep (radar's default queries
    + the prober's needs). Public data; skipped on any failure."""
    try:
        from gateway import radar
        try:
            from agents.prober import probe
            needs = list(probe.DEFAULT_NEEDS)
        except Exception:
            needs = []
        out: dict[str, tuple[str, str]] = {}
        for q in list(dict.fromkeys(needs + radar.SWEEP_QUERIES)):
            try:
                for c in radar.parse_resources(radar.fetch_bazaar(q)):
                    if c["pay_to"] and c["pay_to"] not in out:
                        out[c["pay_to"]] = (c["url"].split("/")[2], c["name"])
            except Exception:
                continue
        return out
    except Exception as e:  # pragma: no cover
        print(f"  (bazaar sweep skipped: {e})", file=sys.stderr)
        return {}


# ── scoring preview (mirrors radar.decide() once AGE-138 lands) ──────────────

def score_multiplier(row: dict) -> float:
    """What the depth row does to a listing's usage score:
    payers are scaled by payer_quality (Σ weight ÷ payers) and the result is
    multiplied by (0.5 + 0.5 × retention). 1.0 = no change."""
    pq = _f(row.get("payer_quality"))
    ret = _f(row.get("retention"))
    return pq * (0.5 + 0.5 * ret)


def to_depth_rows(rows: list[dict], now_iso: str) -> list[dict]:
    out = []
    for r in rows:
        pt = _lower(r.get("recipient"))
        if not pt.startswith("0x"):
            continue
        out.append({
            "pay_to": pt, "network": NETWORK, "window_days": 30,
            "payers": _i(r.get("payers")), "legs": _i(r.get("legs")),
            "usd": round(_f(r.get("usd")), 6), "mean_leg": round(_f(r.get("mean_leg")), 6),
            "returning_payers": _i(r.get("returning_payers")),
            "retention": round(_f(r.get("retention")), 4),
            "effective_payers": round(_f(r.get("effective_payers")), 3),
            "payer_quality": round(_f(r.get("payer_quality")), 4),
            "prober_share": round(_f(r.get("prober_share")), 4),
            "p50_legs_per_payer": round(_f(r.get("p50_legs_per_payer")), 2),
            "top_payer_share": round(_f(r.get("top_payer_share")), 4),
            "first_leg_at": _ts(r.get("first_leg")) or None,
            "last_leg_at": _ts(r.get("last_leg")) or None,
            "source": "dune", "updated_at": now_iso,
        })
    return out


def upsert_depth(rows: list[dict]) -> int:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not (url and key):
        sys.exit("SUPABASE_URL / SUPABASE_KEY missing (.env) — needed for --write")
    if httpx is None:
        sys.exit("httpx missing — run from the repo venv")
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    n = 0
    with httpx.Client(timeout=60.0) as c:
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            r = c.post(f"{url.rstrip('/')}/rest/v1/provider_depth",
                       headers=h, params={"on_conflict": "pay_to,network"}, json=chunk)
            if r.status_code not in (200, 201, 204):
                sys.exit(f"provider_depth upsert failed: HTTP {r.status_code} {r.text[:300]}\n"
                         "(did you apply db/migrations/provider_map.sql?)")
            n += len(chunk)
    return n


# ── report ───────────────────────────────────────────────────────────────────

def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def report(depth: list[dict], names: dict[str, tuple[str, str]], known: set[str],
           controls: dict[str, str], full: bool) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_pt = {d["pay_to"]: d for d in depth}
    n = len(depth)
    usd_all = sum(d["usd"] for d in depth)
    payers_all = sum(d["payers"] for d in depth)
    eff_all = sum(d["effective_payers"] for d in depth)

    def label(pt: str) -> str:
        host, name = names.get(pt, ("", ""))
        for k, v in CONTROL_HOSTS.items():
            if k in host.lower() or k in name.lower():
                return v
        for lab, addr in controls.items():
            if addr == pt:
                return lab
        if pt == OUR_PAYTO:
            return "AgentPay (us)"
        return host or ""

    def line(d: dict) -> str:
        return (f"| {_short(d['pay_to'], full)} | {label(d['pay_to'])} | {d['payers']} | {d['legs']} | "
                f"${d['usd']:,.2f} | {d['returning_payers']} | {d['retention']:.0%} | "
                f"{d['effective_payers']:.1f} | {d['prober_share']:.0%} | {d['p50_legs_per_payer']:.0f} | "
                f"{d['top_payer_share']:.0%} | ×{score_multiplier(d):.2f} |")

    hdr = ("| payTo | who | payers | legs | usd 30d | returned | retention | eff. payers | prober share | "
           "p50 legs/payer | top payer | score × |\n"
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    # resolvability — the weekly metric hook from AGE-138
    res_w = sum(1 for d in depth if d["pay_to"] in names or d["pay_to"] in known)
    res_usd = sum(d["usd"] for d in depth if d["pay_to"] in names or d["pay_to"] in known)

    # distribution
    rets = [d["retention"] for d in depth]
    probers = [d["prober_share"] for d in depth]
    zero_ret = sum(1 for r in rets if r == 0)
    high_prober = sum(1 for p in probers if p >= 0.5)

    top = sorted(depth, key=lambda d: -d["usd"])[:15]
    ctrl = [d for d in depth if label(d["pay_to"]) and label(d["pay_to"]) != names.get(d["pay_to"], ("", ""))[0]]
    ours = by_pt.get(OUR_PAYTO)

    out = [f"# Payer depth — x402 sellers on Base, 30d (run {today})", "",
           "Source: `tools/sql/payer_depth_x402.sql` on Dune (EIP-3009 `AuthorizationUsed` + "
           "x402scan facilitator relayers; sellers = ≥2 payers, mean leg ≤ $5). "
           "Aggregates per seller only — no payer wallets leave Dune.", "",
           "## Population", "",
           f"- **{n:,} sellers** · {payers_all:,} payer-seller pairs · ${usd_all:,.0f} in 30d",
           f"- Σ effective payers = {eff_all:,.0f} → the market's payer counts are **{eff_all / max(payers_all, 1):.0%}** "
           f"'real' after the one-leg/prober discount",
           f"- median retention {_median(rets):.0%}; **{zero_ret:,} sellers ({zero_ret / max(n, 1):.0%}) have zero returning payers**",
           f"- {high_prober:,} sellers ({high_prober / max(n, 1):.0%}) have a prober share ≥ 50% (most of their payers paid once, everywhere once)",
           f"- **resolvable via Bazaar sweep ∪ service_probes: {res_w / max(n, 1):.0%} of sellers, {res_usd / max(usd_all, 1):.0%} of dollars** "
           f"(denominator: the {n:,} sellers above) — the AGE-138 weekly metric",
           "", "## Top 15 by dollars", "", hdr]
    out += [line(d) for d in top]
    out += ["", "## Calibration controls", "",
            "Goal: the score multiplier must SEPARATE the known-synthetic positive (Cluster) "
            "from credible organic sellers (ApiToll, Otto). Constants under test: one-leg floor 0.2, "
            "retention floor 0.5 (`q × (0.5 + 0.5·retention)`).", "", hdr]
    out += [line(d) for d in sorted(ctrl, key=lambda d: -d["usd"])] or ["| — | no control resolved (pass --control label=0x…) | | | | | | | | | | |"]
    if ours:
        out += ["", "## AgentPay", "", hdr, line(ours)]
    out += ["", "## How the multiplier lands (all sellers)", "",
            "| score × bucket | sellers | share of usd |", "|---|---:|---:|"]
    buckets = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)]
    for lo, hi in buckets:
        sel = [d for d in depth if lo <= score_multiplier(d) < hi]
        out.append(f"| {lo:.2f}–{min(hi, 1.0):.2f} | {len(sel):,} | {sum(d['usd'] for d in sel) / max(usd_all, 1):.0%} |")
    out += ["", "_Generated by tools/payer_depth.py. Re-run: re-execute the saved Dune query in the UI, "
            "then `--use-latest` (no credits). `--write` upserts provider_depth (Supabase)._"]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query-id", type=int, help="Dune saved-query id for tools/sql/payer_depth_x402.sql (digits only)")
    ap.add_argument("--csv", help="CSV export of that query (alternative to the API)")
    ap.add_argument("--use-latest", action="store_true", help="fetch the query's latest cached result (no credits)")
    ap.add_argument("--write", action="store_true", help="upsert provider_depth in Supabase")
    ap.add_argument("--control", action="append", default=[], help="extra control: label=0xaddress")
    ap.add_argument("--no-bazaar", action="store_true", help="skip the live Bazaar sweep used to name sellers")
    ap.add_argument("--full-addresses", action="store_true")
    ap.add_argument("--out", help="report path (default reviews/PAYER_DEPTH_<date>.md)")
    ap.add_argument("--json", action="store_true", help="print the depth rows as JSON too")
    a = ap.parse_args()
    _load_dotenv()

    if a.csv:
        rows = read_csv(a.csv)
    elif a.query_id:
        key = os.environ.get("DUNE_API_KEY")
        if not key:
            sys.exit("DUNE_API_KEY not found in .env")
        rows = dune_run(a.query_id, {}, key, use_latest=a.use_latest)
    else:
        sys.exit("pass --query-id <digits> or --csv <file>")
    now_iso = datetime.now(timezone.utc).isoformat()
    depth = to_depth_rows(rows, now_iso)
    print(f"{len(depth)} sellers from Dune", file=sys.stderr)

    names = {} if a.no_bazaar else bazaar_hosts()
    known = fetch_known_paytos()
    controls = {}
    for c in a.control:
        lab, _, addr = c.partition("=")
        if addr:
            controls[lab.strip()] = _lower(addr)

    md = report(depth, names, known, controls, a.full_addresses)
    out = a.out or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "reviews", f"PAYER_DEPTH_{datetime.now(timezone.utc):%Y-%m-%d}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(md)
    print(md)
    print(f"report → {out}", file=sys.stderr)
    if a.json:
        print(json.dumps(depth, default=str))
    if a.write:
        n = upsert_depth(depth)
        print(f"provider_depth: upserted {n} rows", file=sys.stderr)


if __name__ == "__main__":
    main()
