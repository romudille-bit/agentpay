#!/usr/bin/env python3
"""
payer_depth.py — AGE-138 repeat-depth ranking (AGE-133 phase 0): pull the
per-seller payer shape from Dune (tools/sql/payer_depth_x402.sql), write the
calibration report, and (optionally) upsert `provider_depth` in Supabase so
verified_route can weight payers by whether they came back.

Two sources, same output shape:

  x402scan (DEFAULT, keyless) — the public tRPC API behind x402scan.com
    (Merit-Systems/x402scan, open source): per-recipient transfers with the
    real payer (`public.transfers.list`, 30d, Base) + batched buyer stats
    (`public.buyers.all.list`: tx_count + unique_sellers → market-wide fanout).
    Same denominator as the AGE-140 census v2 (facilitator-relayed USDC
    transfers; their registry IS our relayer list). Runs for the payTos we
    care about: Bazaar sweep ∪ service_probes ∪ our own ∪ --top N market head.
    ~2 requests per payTo, self-throttled. No account, no credits — Dune's
    free plan goes view-only on 2026-09-10, so this is the recurring path.

  Dune (--query-id / --csv) — tools/sql/payer_depth_x402.sql over the WHOLE
    market (≈1–2k sellers). Cross-check while it is still free.

    source venv/bin/activate
    python3 tools/payer_depth.py                          # x402scan, report only
    python3 tools/payer_depth.py --top 25 --write         # + market head, upsert provider_depth
    python3 tools/payer_depth.py --query-id <digits> --use-latest   # Dune cross-check
    python3 tools/payer_depth.py --csv ~/Downloads/payer_depth.csv  # Dune CSV export

SUPABASE_URL / SUPABASE_KEY (for --write and service_probes) and DUNE_API_KEY
(Dune path only) are read from ../.env. Aggregates only are written — no
payer wallet reaches Supabase or the report. Addresses are shortened unless
--full-addresses (data-sharing rule).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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
# Off-catalog sellers we can name (public on x402scan; identified in the
# AGE-140 census by payers/dollars). Same convention as radar.KNOWN_TRUSTED.
KNOWN_SELLERS = {
    "0xe9030014f5dae217d0a152f02a043567b16c1abf": "BlockRun",
    "0x68396bd35874695ad86cd29410bd80a550991a2b": "Cluster (synthetic)",
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


# ── x402scan (keyless) ───────────────────────────────────────────────────────

X402SCAN_TRPC = "https://www.x402scan.com/api/trpc"
X402SCAN_UA = "agentpay-radar/payer_depth (+https://agentpay.tools)"
_THROTTLE_S = 0.35            # be a polite guest on a free public API
_PAGE = 500
_MAX_LEGS = 5000              # per payTo; a 7.5M-leg fleet is SAMPLED (most recent), and says so


def x402scan(proc: str, inp: dict, retries: int = 3) -> dict:
    """GET one tRPC procedure. Returns the `json` payload; raises on failure."""
    q = urllib.parse.quote(json.dumps({"json": inp}))
    req = urllib.request.Request(f"{X402SCAN_TRPC}/{proc}?input={q}",
                                 headers={"User-Agent": X402SCAN_UA, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            time.sleep(_THROTTLE_S)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())["result"]["data"]["json"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def x402scan_top_sellers(n: int, chain: str = "base") -> list[dict]:
    """Top-n sellers by tx_count (30d) — the market head, so the synthetic
    controls are in the table without knowing their addresses."""
    out: list[dict] = []
    page = 0
    while len(out) < n:
        res = x402scan("public.sellers.all.list",
                       {"timeframe": 30, "chain": chain,
                        "sorting": {"id": "tx_count", "desc": True},
                        "pagination": {"page": page, "page_size": min(_PAGE, n - len(out))}})
        out.extend(res.get("items") or [])
        if not res.get("hasNextPage"):
            break
        page += 1
    return out[:n]


def x402scan_all_sellers(chain: str = "base", page_size: int = _PAGE, max_pages: int = 200) -> list[dict]:
    """The whole 30d seller table (recipient, tx_count, unique_buyers,
    total_amount) — the denominator for the resolvable-share metric."""
    out: list[dict] = []
    page = 0
    while page < max_pages:
        res = x402scan("public.sellers.all.list",
                       {"timeframe": 30, "chain": chain,
                        "sorting": {"id": "total_amount", "desc": True},
                        "pagination": {"page": page, "page_size": page_size}})
        out.extend(res.get("items") or [])
        if not res.get("hasNextPage"):
            break
        page += 1
    return out


def x402scan_seller_totals(paytos: list[str], chain: str = "base") -> dict[str, dict]:
    """True 30d totals per payTo (tx_count, unique_buyers, total_amount) from
    the sellers MV, batched 50 addresses per call."""
    out: dict[str, dict] = {}
    for i in range(0, len(paytos), 50):
        batch = paytos[i:i + 50]
        page = 0
        while True:
            res = x402scan("public.sellers.all.list",
                           {"timeframe": 30, "chain": chain, "recipients": {"include": batch},
                            "pagination": {"page": page, "page_size": _PAGE}})
            for it in res.get("items") or []:
                out[_lower(it.get("recipient"))] = it
            if not res.get("hasNextPage"):
                break
            page += 1
    return out


def x402scan_legs(payto: str, chain: str = "base", max_legs: int = _MAX_LEGS) -> tuple[list[dict], bool]:
    """(transfers to `payto` in 30d, newest first, truncated?)."""
    legs: list[dict] = []
    page = 0
    truncated = False
    while True:
        res = x402scan("public.transfers.list",
                       {"timeframe": 30, "chain": chain, "recipients": {"include": [payto]},
                        "sorting": {"id": "block_timestamp", "desc": True},
                        "pagination": {"page": page, "page_size": _PAGE}})
        legs.extend(res.get("items") or [])
        if not res.get("hasNextPage"):
            break
        if len(legs) >= max_legs:
            truncated = True
            break
        page += 1
    return legs, truncated


def x402scan_buyer_shape(senders: list[str], chain: str = "base") -> dict[str, dict]:
    """{sender: {tx_count, unique_sellers}} — market-wide, batched 50 per call."""
    out: dict[str, dict] = {}
    for i in range(0, len(senders), 50):
        batch = senders[i:i + 50]
        page = 0
        while True:
            res = x402scan("public.buyers.all.list",
                           {"timeframe": 30, "chain": chain, "senders": {"include": batch},
                            "pagination": {"page": page, "page_size": _PAGE}})
            for it in res.get("items") or []:
                out[_lower(it.get("sender"))] = it
            if not res.get("hasNextPage"):
                break
            page += 1
    return out


def payer_weight(legs_here: int, fanout: float) -> float:
    """AGE-138 payer weight — identical to the CASE in payer_depth_x402.sql."""
    if legs_here >= 2:
        return 1.0
    return 0.2 + 0.8 * (1.0 - min(max(fanout, 0.0), 1.0))


def _p50(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def depth_from_x402scan(paytos: list[str], chain: str = "base", max_legs: int = _MAX_LEGS,
                        log=print) -> list[dict]:
    """Build Dune-shaped rows (recipient, payers, legs, …) for `paytos` from
    x402scan. Returns [] entries for payTos with no 30d transfers omitted."""
    paytos = list(dict.fromkeys(_lower(p) for p in paytos if p))
    totals = x402scan_seller_totals(paytos, chain)
    rows: list[dict] = []
    all_senders: set[str] = set()
    per_payto: dict[str, dict[str, dict]] = {}
    for i, pt in enumerate(paytos, 1):
        legs, truncated = x402scan_legs(pt, chain, max_legs)
        if not legs:
            continue
        pairs: dict[str, dict] = {}
        for lg in legs:
            snd = _lower(lg.get("sender"))
            usd = _f(lg.get("amount")) / (10 ** int(lg.get("decimals") or 6))
            ts = str(lg.get("block_timestamp") or "")
            p = pairs.setdefault(snd, {"legs": 0, "usd": 0.0, "first": ts, "last": ts})
            p["legs"] += 1
            p["usd"] += usd
            p["first"] = min(p["first"], ts)
            p["last"] = max(p["last"], ts)
        per_payto[pt] = {"pairs": pairs, "truncated": truncated, "sampled_legs": len(legs)}
        all_senders.update(pairs)
        log(f"  [{i}/{len(paytos)}] {pt[:10]}… {len(legs)} legs · {len(pairs)} payers"
            + (" · SAMPLED" if truncated else ""), file=sys.stderr)
    shape = x402scan_buyer_shape(sorted(all_senders), chain)
    for pt, info in per_payto.items():
        pairs = info["pairs"]
        weights = []
        for snd, p in pairs.items():
            sh = shape.get(snd) or {}
            txc = _i(sh.get("tx_count")) or p["legs"]
            fan = (_i(sh.get("unique_sellers")) or 1) / max(txc, 1)
            weights.append(payer_weight(p["legs"], fan))
        payers = len(pairs)
        legs_n = sum(p["legs"] for p in pairs.values())
        usd = sum(p["usd"] for p in pairs.values())
        returning = sum(1 for p in pairs.values() if p["legs"] >= 2)
        tot = totals.get(pt) or {}
        rows.append({
            "recipient": pt,
            "payers": payers,
            "legs": legs_n,
            "usd": usd,
            "mean_leg": usd / max(legs_n, 1),
            "returning_payers": returning,
            "retention": returning / max(payers, 1),
            "effective_payers": sum(weights),
            "payer_quality": sum(weights) / max(payers, 1),
            "prober_share": sum(1 for w in weights if w < 0.5) / max(payers, 1),
            "p50_legs_per_payer": _p50([p["legs"] for p in pairs.values()]),
            "top_payer_share": max(p["usd"] for p in pairs.values()) / max(usd, 1e-9),
            "first_leg": min(p["first"] for p in pairs.values()),
            "last_leg": max(p["last"] for p in pairs.values()),
            # provenance — true totals vs what the sample saw
            "total_legs_30d": _i(tot.get("tx_count")) or legs_n,
            "total_payers_30d": _i(tot.get("unique_buyers")) or payers,
            "sampled": info["truncated"],
        })
    return rows


# ── scoring preview (mirrors radar.decide() once AGE-138 lands) ──────────────

def score_multiplier(row: dict) -> float:
    """What the depth row does to a listing's usage score:
    payers are scaled by payer_quality (Σ weight ÷ payers) and the result is
    multiplied by (0.5 + 0.5 × retention). 1.0 = no change."""
    pq = _f(row.get("payer_quality"))
    ret = _f(row.get("retention"))
    return pq * (0.5 + 0.5 * ret)


def to_depth_rows(rows: list[dict], now_iso: str, source: str = "dune") -> list[dict]:
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
            "source": source, "updated_at": now_iso,
        })
        if "total_legs_30d" in r:
            out[-1]["total_legs_30d"] = _i(r.get("total_legs_30d"))
            out[-1]["total_payers_30d"] = _i(r.get("total_payers_30d"))
            out[-1]["sampled"] = bool(r.get("sampled"))
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
           controls: dict[str, str], full: bool, source: str = "dune",
           market: list[dict] | None = None) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_pt = {d["pay_to"]: d for d in depth}
    n = len(depth)
    usd_all = sum(d["usd"] for d in depth)
    payers_all = sum(d["payers"] for d in depth)
    eff_all = sum(d["effective_payers"] for d in depth)

    def label(pt: str) -> str:
        if pt in KNOWN_SELLERS:
            return KNOWN_SELLERS[pt]
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
        legs_s = (f"{d['legs']}" if not d.get("sampled")
                  else f"{d['legs']} of {d.get('total_legs_30d', 0):,}†")
        # fleet shape from TRUE 30d totals when the source has them (a sampled
        # p50 under-reads a 7.5M-leg fleet); sample-based otherwise.
        lpp = (d["total_legs_30d"] / max(d.get("total_payers_30d") or 0, 1)
               if d.get("total_legs_30d") else d["legs"] / max(d["payers"], 1))
        return (f"| {_short(d['pay_to'], full)} | {label(d['pay_to'])} | {d['payers']} | {legs_s} | "
                f"${d['usd']:,.2f} | {d['returning_payers']} | {d['retention']:.0%} | "
                f"{d['effective_payers']:.1f} | {d['prober_share']:.0%} | {lpp:,.0f} | "
                f"{d['top_payer_share']:.0%} | ×{score_multiplier(d):.2f} |")

    hdr = ("| payTo | who | payers | legs | usd 30d | returned | retention | eff. payers | prober share | "
           "legs/payer 30d | top payer | score × |\n"
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
    ctrl = [d for d in ctrl if d["pay_to"] in KNOWN_SELLERS or "(" in label(d["pay_to"])]
    ours = by_pt.get(OUR_PAYTO)

    src_line = ("Source: x402scan public API (facilitator-relayed USDC transfers on Base, 30d; "
                "payTos = Bazaar sweep ∪ service_probes ∪ ours ∪ market head). Payers with more "
                "than the per-payTo sample cap are SAMPLED on their most recent legs (marked †). "
                "Aggregates per seller only — no payer wallet leaves the run."
                if source == "x402scan" else
                "Source: `tools/sql/payer_depth_x402.sql` on Dune (EIP-3009 `AuthorizationUsed` + "
                "x402scan facilitator relayers; sellers = ≥2 payers, mean leg ≤ $5). "
                "Aggregates per seller only — no payer wallets leave Dune.")
    out = [f"# Payer depth — x402 sellers on Base, 30d (run {today})", "", src_line, "",
           "## Population", "",
           f"- **{n:,} sellers** · {payers_all:,} payer-seller pairs · ${usd_all:,.0f} in 30d",
           f"- Σ effective payers = {eff_all:,.0f} → the market's payer counts are **{eff_all / max(payers_all, 1):.0%}** "
           f"'real' after the one-leg/prober discount",
           f"- median retention {_median(rets):.0%}; **{zero_ret:,} sellers ({zero_ret / max(n, 1):.0%}) have zero returning payers**",
           f"- {high_prober:,} sellers ({high_prober / max(n, 1):.0%}) have a prober share ≥ 50% (most of their payers paid once, everywhere once)",
           f"- resolvable via Bazaar sweep ∪ service_probes within this table: {res_w / max(n, 1):.0%} of sellers, "
           f"{res_usd / max(usd_all, 1):.0%} of dollars (denominator: the {n:,} sellers above)"]
    if market:
        m_n = len(market)
        m_usd = sum(_f(x.get("total_amount")) / 1e6 for x in market)
        m_res = [x for x in market if _lower(x.get("recipient")) in names or _lower(x.get("recipient")) in known]
        m_res_usd = sum(_f(x.get("total_amount")) / 1e6 for x in m_res)
        top10 = market[:10]
        top10_res = sum(1 for x in top10 if _lower(x.get("recipient")) in names or _lower(x.get("recipient")) in known)
        out += [f"- **WEEKLY METRIC — resolvable share of the whole x402 market on Base (30d, x402scan: "
                f"{m_n:,} sellers / ${m_usd:,.0f}): {len(m_res) / max(m_n, 1):.1%} of sellers, "
                f"{m_res_usd / max(m_usd, 1):.1%} of dollars**; {top10_res} of the top-10 sellers by dollars are resolvable"]
    out += ["", "## Top 15 by dollars", "", hdr]
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
    out += ["", "_Generated by tools/payer_depth.py (source: " + source + "). "
            "`--write` upserts provider_depth (Supabase); `--query-id`/`--csv` = Dune cross-check._"]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query-id", type=int, help="Dune cross-check: saved-query id for tools/sql/payer_depth_x402.sql (digits only)")
    ap.add_argument("--csv", help="Dune cross-check: CSV export of that query")
    ap.add_argument("--use-latest", action="store_true", help="Dune: fetch the query's latest cached result (no credits)")
    ap.add_argument("--top", type=int, default=0, help="x402scan: also include the top-N sellers by tx_count (market head / controls)")
    ap.add_argument("--payto", action="append", default=[], help="x402scan: extra payTo(s) to include")
    ap.add_argument("--max-legs", type=int, default=_MAX_LEGS, help="x402scan: per-payTo sample cap (newest first)")
    ap.add_argument("--market", action="store_true", help="x402scan: also pull the whole 30d seller table for the market-wide resolvable-share metric (~45 calls)")
    ap.add_argument("--write", action="store_true", help="upsert provider_depth in Supabase")
    ap.add_argument("--control", action="append", default=[], help="extra control: label=0xaddress")
    ap.add_argument("--no-bazaar", action="store_true", help="skip the live Bazaar sweep used to name sellers")
    ap.add_argument("--full-addresses", action="store_true")
    ap.add_argument("--out", help="report path (default reviews/PAYER_DEPTH_<date>.md)")
    ap.add_argument("--json", action="store_true", help="print the depth rows as JSON too")
    a = ap.parse_args()
    _load_dotenv()

    names = {} if a.no_bazaar else bazaar_hosts()
    known = fetch_known_paytos()
    if a.csv:
        source, rows = "dune", read_csv(a.csv)
    elif a.query_id:
        key = os.environ.get("DUNE_API_KEY")
        if not key:
            sys.exit("DUNE_API_KEY not found in .env")
        source, rows = "dune", dune_run(a.query_id, {}, key, use_latest=a.use_latest)
    else:
        source = "x402scan"
        paytos = set(names) | set(known) | {OUR_PAYTO} | {_lower(p) for p in a.payto}
        if a.top:
            head = x402scan_top_sellers(a.top)
            paytos |= {_lower(it.get("recipient")) for it in head}
            print(f"market head: top {len(head)} sellers by tx_count added", file=sys.stderr)
        print(f"x402scan: {len(paytos)} payTos to pull", file=sys.stderr)
        rows = depth_from_x402scan(sorted(paytos), max_legs=a.max_legs)
    now_iso = datetime.now(timezone.utc).isoformat()
    depth = to_depth_rows(rows, now_iso, source)
    print(f"{len(depth)} sellers from {source}", file=sys.stderr)
    controls = {}
    for c in a.control:
        lab, _, addr = c.partition("=")
        if addr:
            controls[lab.strip()] = _lower(addr)

    market = None
    if a.market:
        market = x402scan_all_sellers()
        print(f"market: {len(market)} sellers in the 30d table", file=sys.stderr)
    md = report(depth, names, known, controls, a.full_addresses, source, market)
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
