"""
radar.py — x402 discovery core for the Arbitrum x402 Radar.

Buyer-side discovery over the x402 marketplace, refactored out of the bundled
router (`plugins/agentpay/bin/agentpay-route`) so the gateway can import the same
DISCOVER → DECIDE pipeline and serve it at `GET /discovery/arbitrum`.

Design: the parsing/filtering/ranking are **pure functions** (no I/O) so they're
unit-testable against captured Bazaar payloads. The single I/O function
(`fetch_bazaar`) takes an injectable fetcher, so the async gateway can pass an
httpx-based getter while the sync CLI keeps urllib.

Pipeline:
    DISCOVER  fetch_bazaar(need) → parse_resources(data) → candidates
    FILTER    filter_chain(candidates, chain) → only the requested chain(s)
    DECIDE    decide(candidates, budget) → junk-filter → usage-quality rank →
              budget gate → price tiebreak → (scored, recommendation)

"Cheapest that's real and actually used," never just "cheapest."
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Optional

BAZAAR_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
UA = "agentpay-radar/0.1 (+https://agentpay.tools)"

# Known stub-factory payTo addresses (from the 2026-06-03 competitor scan). One
# wallet stamping dozens of "distinct" tools = downrank the whole family. Prefix
# match (lowercased).
KNOWN_FACTORIES = {"0x2bb72231eed3".lower()}  # Orbis


# ── Chain identity ─────────────────────────────────────────────────────────────
# Bazaar advertises each option's network as a CAIP-2 id (e.g. "eip155:8453").
# Map friendly chain keys (and the "arbitrum-stack" group) to the CAIP-2 ids we
# accept. Robinhood Chain (46630) is an Arbitrum Orbit/Nitro chain, so it's part
# of the stack — and notably Bazaar/CDP does NOT index it, so the only place it
# shows up in a Radar is via our own crawl (Day 2), not this Bazaar feed.
CHAIN_NETWORKS: dict[str, set[str]] = {
    "base":              {"eip155:8453"},
    "base-sepolia":      {"eip155:84532"},
    "arbitrum":          {"eip155:42161"},
    "arbitrum-one":      {"eip155:42161"},
    "arbitrum-sepolia":  {"eip155:421614"},
    "robinhood":         {"eip155:46630"},
    "robinhood-testnet": {"eip155:46630"},
    # The headline group: every Arbitrum-stack chain (Arbitrum One + Sepolia +
    # Robinhood Chain). This is what `GET /discovery/arbitrum` surfaces.
    "arbitrum-stack":    {"eip155:42161", "eip155:421614", "eip155:46630"},
}

# Friendly aliases that may appear in a candidate's `network` field instead of a
# CAIP-2 id, normalized to CAIP-2 so filtering is uniform.
_NETWORK_ALIASES: dict[str, str] = {
    "base": "eip155:8453",
    "base-mainnet": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "arbitrum": "eip155:42161",
    "arbitrum-one": "eip155:42161",
    "arbitrum-mainnet": "eip155:42161",
    "arbitrum-sepolia": "eip155:421614",
    "robinhood": "eip155:46630",
    "robinhood-testnet": "eip155:46630",
}


def normalize_network(network: str) -> str:
    """Return the CAIP-2 id for a candidate's network string.

    Accepts an already-CAIP-2 value ("eip155:42161") unchanged, or maps a
    friendly alias ("arbitrum-one") to CAIP-2. Unknown values pass through
    lowercased so an unexpected label never crashes the filter.
    """
    n = (network or "").strip().lower()
    return _NETWORK_ALIASES.get(n, n)


def networks_for(chain: Optional[str]) -> Optional[set[str]]:
    """Resolve a chain key to its set of acceptable CAIP-2 ids.

    Returns None when `chain` is falsy (meaning "no filter — all chains"). An
    unknown chain key is treated as a literal CAIP-2 id so callers can pass a
    raw network directly.
    """
    if not chain:
        return None
    key = chain.strip().lower()
    if key in CHAIN_NETWORKS:
        return set(CHAIN_NETWORKS[key])
    return {normalize_network(key)}


# ── DISCOVER ───────────────────────────────────────────────────────────────────
def _default_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:  # noqa: S310 (trusted host)
        return json.loads(r.read().decode())


def fetch_bazaar(need: str, fetch: Callable[[str], dict] = _default_get) -> dict:
    """Query Bazaar discovery for `need`. `fetch` is injectable for testing/async."""
    return fetch(f"{BAZAAR_URL}?query={urllib.parse.quote(need)}")


def parse_resources(data: dict) -> list[dict]:
    """Normalize + dedup a Bazaar discovery payload into candidate dicts. Pure."""
    out: list[dict] = []
    seen: set[str] = set()
    for r in (data or {}).get("resources", []):
        res = r.get("resource")
        rd = res if isinstance(res, dict) else {}
        url = res if isinstance(res, str) else rd.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        accepts = r.get("accepts") or rd.get("accepts") or [{}]
        a = accepts[0] if accepts else {}
        try:
            amount_atomic = int(a.get("amount", 0))
            # 0/missing amount = "no usable price", NOT free — otherwise a
            # stub with no price wins every price tiebreak.
            price = (Decimal(amount_atomic) / Decimal("1000000")) if amount_atomic > 0 else None
        except (ValueError, TypeError):
            price = None
        ext = (r.get("extensions") or rd.get("extensions") or {}).get("bazaar") or {}
        out_schema = a.get("outputSchema") or ext.get("info", {}).get("output") or ext.get("schema")
        q = r.get("quality") or {}
        out.append({
            "name": r.get("serviceName") or rd.get("serviceName") or url.rsplit("/", 1)[-1],
            "url": url,
            "price_usd": price,
            "network": a.get("network", ""),
            "network_caip2": normalize_network(a.get("network", "")),
            "pay_to": (a.get("payTo") or "").lower(),
            "tags": r.get("tags") or rd.get("tags") or [],
            "has_schema": bool(out_schema) and out_schema != {},
            "calls30d": int(q.get("l30DaysTotalCalls", 0) or 0),
            "payers30d": int(q.get("l30DaysUniquePayers", 0) or 0),
            "last_called": q.get("lastCalledAt"),
            # Raw first `accepts` entry so verified_route can hand the buyer a
            # ready-to-pay x402 challenge without a second fetch.
            "accepts": a,
        })
    return out


def filter_chain(cands: Iterable[dict], chain: Optional[str]) -> list[dict]:
    """Keep only candidates whose network is in the requested chain group.

    `chain=None` (or unknown-empty) returns everything. Matching is on the
    normalized CAIP-2 id, so "arbitrum", "arbitrum-one", and "eip155:42161"
    all behave the same.
    """
    nets = networks_for(chain)
    if nets is None:
        return list(cands)
    return [c for c in cands if c.get("network_caip2") in nets]


def discover(need: str, chain: Optional[str] = None,
             fetch: Callable[[str], dict] = _default_get) -> list[dict]:
    """DISCOVER convenience: fetch Bazaar, parse, optionally filter by chain."""
    return filter_chain(parse_resources(fetch_bazaar(need, fetch=fetch)), chain)


# ── DECIDE ─────────────────────────────────────────────────────────────────────
def _recency_days(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def decide(cands: list[dict], remaining: Decimal,
           usage_aware: bool = False) -> tuple[list[dict], Optional[dict]]:
    """Filter + rank. Returns (scored_with_verdicts, recommendation). Pure.

    Stages: junk-filter (no schema = stub; factory fingerprint) → budget gate →
    usage-quality score (unique_payers×3 + calls + recency bonus, factory
    downrank) → sort by quality desc, price asc.

    `usage_aware` (verified_route uses True): a wallet with many listings is only
    a "factory" if those listings are MOSTLY UNPROVEN. A trustworthy multi-product
    provider (e.g. CMC) whose endpoints each have real payers is NOT a factory and
    is never downranked for breadth. Known-trusted payTo addresses are always
    exempt; known factory prefixes are always factories. Default False preserves
    the legacy count-only behavior the Arbitrum radar + its tests rely on.
    """
    names_per_payto: dict[str, set] = {}
    listings_per_payto: dict[str, list] = {}
    for c in cands:
        if c["pay_to"]:
            names_per_payto.setdefault(c["pay_to"], set()).add(c["name"])
            listings_per_payto.setdefault(c["pay_to"], []).append(c)

    scored: list[dict] = []
    for c in cands:
        flags: list[str] = []
        dropped, reason = False, ""

        # Stage 2 — junk filter
        if not c["has_schema"]:
            dropped, reason = True, "no usable schema (stub)"
        pt = c["pay_to"]
        known_factory = any(pt.startswith(f) for f in KNOWN_FACTORIES)
        known_trusted = any(pt.startswith(t) for t in KNOWN_TRUSTED)
        cluster = len(names_per_payto.get(pt, set())) >= FACTORY_MIN_NAMES
        if usage_aware and cluster and not known_factory:
            # Decide on USAGE, not raw endpoint count: a cluster is only a factory
            # when most of its listings are unproven. Real providers keep breadth.
            grp = listings_per_payto.get(pt, [])
            proven = sum(1 for x in grp if x["payers30d"] >= PROVEN_PAYERS)
            cluster = (proven / max(len(grp), 1)) < 0.5
        is_factory = (known_factory or cluster) and not known_trusted
        if is_factory:
            flags.append("factory")

        # budget gate
        if not dropped and (c["price_usd"] is None or c["price_usd"] > remaining):
            dropped, reason = True, (
                f"{c['price_usd']} > budget {remaining}"
                if c["price_usd"] is not None else "no usable price")

        # Stage 3 — usage quality
        rec_days = _recency_days(c["last_called"])
        q = c["payers30d"] * 3 + c["calls30d"]
        if rec_days is not None and rec_days <= 7:
            q += 5
        if is_factory:
            q = q // 4
        if c["payers30d"] == 0 and c["calls30d"] == 0:
            flags.append("unproven(0/0)")

        scored.append({**c, "flags": flags, "dropped": dropped,
                       "drop_reason": reason, "quality": q, "rec_days": rec_days})

    survivors = [s for s in scored if not s["dropped"]]
    survivors.sort(key=lambda s: (-s["quality"], s["price_usd"]))
    recommendation = survivors[0] if survivors else None
    return scored, recommendation


def rank_from_payload(data: dict, need: str, budget: Decimal,
                      chain: Optional[str] = None,
                      extra: Optional[Iterable[dict]] = None) -> dict:
    """Assemble a JSON-able Radar result from an already-fetched Bazaar payload.

    Pure (no I/O) so the async gateway can fetch with httpx and hand the payload
    here. `extra` lets the Robinhood crawler (Day 2b) inject candidates Bazaar
    can't see; they flow through the same chain filter + ranking.
    """
    cands = filter_chain(parse_resources(data), chain)
    if extra:
        cands = cands + filter_chain(list(extra), chain)
    scored, rec = decide(cands, budget)
    survivors = [s for s in scored if not s["dropped"]]
    survivors.sort(key=lambda s: (-s["quality"], s["price_usd"]))
    return {
        "need": need,
        "chain": chain,
        "budget_usd": str(budget),
        "count": len(cands),
        "results": [_public(s) for s in survivors],
        "recommendation": _public(rec) if rec else None,
    }


def rank(need: str, budget: Decimal, chain: Optional[str] = None,
         fetch: Callable[[str], dict] = _default_get) -> dict:
    """End-to-end (sync): fetch Bazaar → rank_from_payload. Used by the CLI path.

    The async gateway path calls `rank_from_payload` directly with an httpx fetch.
    """
    return rank_from_payload(fetch_bazaar(need, fetch=fetch), need, budget, chain)


def _public(s: Optional[dict]) -> Optional[dict]:
    """Project a scored candidate down to the public discovery shape."""
    if not s:
        return None
    out = {
        "name": s["name"],
        "url": s["url"],
        "price_usd": (str(s["price_usd"]) if s["price_usd"] is not None else None),
        "network": s["network_caip2"] or s["network"],
        "pay_to": s["pay_to"],
        "tags": s["tags"],
        "calls30d": s["calls30d"],
        "payers30d": s["payers30d"],
        "quality": s["quality"],
        "flags": s["flags"],
    }
    if s.get("collapsed_siblings"):
        out["collapsed_siblings"] = s["collapsed_siblings"]
    return out


# ── verified_route — the PAID trust oracle ($0.01) ──────────────────────────────
# The free `route`/`/discovery` path ranks ONE Bazaar query. verified_route does
# the work an agent can't do in a single query: sweep the whole catalog across
# many terms, collapse sybil/factory clusters (one wallet stamping many "distinct"
# tools → one entry), and return the genuinely-distinct, actually-used survivors.
# All pure functions below so they're unit-testable against captured payloads.

# Default sweep terms for comprehensive discovery. Bazaar returns a slice per
# query, so a fixed broad sweep + dedup approximates the full catalog.
SWEEP_QUERIES = [
    "api", "data", "crypto", "price", "ai", "search", "weather", "stock",
    "news", "image", "trade", "defi", "token", "finance", "llm", "agent",
]

# Sybil/spam detection is USAGE-based, not endpoint-count based. A trustworthy
# provider (e.g. CMC) may legitimately list many endpoints; what marks spam is a
# long tail of barely-used listings under one wallet — not breadth itself.
FACTORY_MIN_NAMES = 3   # min distinct listings under one wallet to even scrutinize it
PROVEN_PAYERS     = 10  # a listing with >= this many 30d unique payers is a "real" tool
SYBIL_TAIL_MIN    = 5   # collapse a wallet's UNPROVEN (<PROVEN_PAYERS) listings when >= this many

# Known-trusted payTo addresses — never flagged factory, never collapsed, no
# matter how many endpoints they list. Curated allowlist of reputable providers.
# Prefix match (lowercased), same convention as KNOWN_FACTORIES.
KNOWN_TRUSTED = {
    "0x271189c860db25bc43173b0335784ad68a680908".lower(),  # CoinMarketCap x402
}


def merge_resources(payloads: Iterable[dict]) -> dict:
    """Concatenate `resources` from many Bazaar payloads into one payload.

    parse_resources dedups by url downstream, so plain concatenation is enough.
    """
    out: list[dict] = []
    for d in payloads:
        out.extend((d or {}).get("resources", []) or [])
    return {"resources": out}


def collapse_sybils(survivors: list[dict]) -> tuple[list[dict], dict]:
    """Fold a wallet's UNPROVEN tail (not its breadth) into one entry.

    Decision is usage-based, not count-based: within a wallet's listings, the
    PROVEN ones (>= PROVEN_PAYERS unique payers) ALWAYS stay visible — a real
    multi-product provider like CMC keeps every used endpoint. Only when a wallet
    carries a long tail of >= SYBIL_TAIL_MIN unproven listings do those fold into
    a single representative. Known-trusted wallets are never collapsed at all.
    Returns (kept_sorted, stats). Pure.
    """
    by_payto: dict[str, list[dict]] = {}
    no_payto: list[dict] = []
    for s in survivors:
        pt = s.get("pay_to")
        if pt:
            by_payto.setdefault(pt, []).append(s)
        else:
            no_payto.append(s)

    kept: list[dict] = list(no_payto)
    collapsed = 0
    biggest = {"pay_to": None, "listings": 0}   # biggest collapsed unproven tail (= spam size)
    for pt, group in by_payto.items():
        trusted = any(pt.startswith(t) for t in KNOWN_TRUSTED)
        unproven = [s for s in group if s["payers30d"] < PROVEN_PAYERS]
        proven = [s for s in group if s["payers30d"] >= PROVEN_PAYERS]
        if not trusted and len(unproven) >= SYBIL_TAIL_MIN:
            kept.extend(proven)                       # real endpoints survive untouched
            best = max(unproven, key=lambda s: s["quality"])
            kept.append({**best, "collapsed_siblings": len(unproven) - 1})
            collapsed += len(unproven) - 1
            if len(unproven) > biggest["listings"]:
                biggest = {"pay_to": pt, "listings": len(unproven)}
        else:
            kept.extend(group)                        # trusted, or no spam tail → keep all

    kept.sort(key=lambda s: (-s["quality"],
                             s["price_usd"] if s["price_usd"] is not None else Decimal("999")))
    stats = {
        "unique_wallets": len(by_payto) + (1 if no_payto else 0),
        "sybil_collapsed": collapsed,
        "biggest_factory": biggest if biggest["listings"] > 0 else None,
    }
    return kept, stats


def _ready_to_pay(s: Optional[dict]) -> Optional[dict]:
    """The buyer-facing 'how to pay this' block for the recommendation."""
    if not s:
        return None
    return {"url": s["url"], "network": s["network_caip2"] or s["network"],
            "price_usd": (str(s["price_usd"]) if s["price_usd"] is not None else None),
            "accepts": s.get("accepts") or {}}


def verified_route_from_payloads(payloads: list[dict], need: str, budget: Decimal,
                                 chain: Optional[str] = None,
                                 extra: Optional[Iterable[dict]] = None) -> dict:
    """Assemble the paid verified_route result from swept Bazaar payloads. Pure.

    DISCOVER (merge+dedup many queries) → FILTER (chain) → DECIDE (junk/factory/
    rank over the FULL set) → COLLAPSE (sybil clusters → one entry) → recommend.
    """
    merged = merge_resources(payloads)
    cands = filter_chain(parse_resources(merged), chain)
    if extra:
        cands = cands + filter_chain(list(extra), chain)

    scored, _ = decide(cands, budget, usage_aware=True)
    survivors = [s for s in scored if not s["dropped"]]
    kept, stats = collapse_sybils(survivors)
    rec = kept[0] if kept else None

    rec_pub = _public(rec)
    if rec_pub:
        rec_pub["ready_to_pay"] = _ready_to_pay(rec)

    return {
        "need": need,
        "chain": chain,
        "budget_usd": str(budget),
        "recommendation": rec_pub,
        "survivors": [_public(s) for s in kept],
        "catalog": {
            "scanned": len(cands),
            "after_vetting": len(survivors),
            "real_providers": len(kept),
            "unique_wallets": stats["unique_wallets"],
            "sybil_collapsed": stats["sybil_collapsed"],
            "biggest_factory": stats["biggest_factory"],
        },
        "vetting": (
            f"swept {len(payloads)} queries → {len(cands)} listings → "
            f"collapsed {stats['sybil_collapsed']} sybil listings → "
            f"{len(kept)} real providers"
        ),
    }
