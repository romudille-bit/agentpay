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
import re
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
            # Searchable text for the relevance tier (AGE-43); Bazaar carries a
            # description on the listing, the resource dict, or the extension.
            "description": (r.get("description") or rd.get("description")
                            or (ext.get("info") or {}).get("description") or ""),
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


# Delivery-failure flags from the Prober's score rows (mirrors of
# agents/prober/probe.py constants — kept literal so radar.py stays importable
# without the agents package). AGE-11 policy: BOTH hard-drop a service from
# recommendation (buyer protection is immediate), but only the CONFIRMED flag
# (>= 2 paid_but_no_data on separate runs) carries the public accusation in
# why-text — a single failure may be a transient outage.
FLAG_NO_DELIVERY = "took_payment_no_delivery"
FLAG_NO_DELIVERY_UNCONFIRMED = "no_delivery_unconfirmed"
_NO_DELIVERY_FLAGS = {FLAG_NO_DELIVERY, FLAG_NO_DELIVERY_UNCONFIRMED}

# [MR-2] single-payer wash volume: calls capped at this multiple of unique
# payers in usage_q, and flagged above it. 342 calls from 1 payer ranks like
# ~20 calls, not like a popular tool.
CALLS_PER_PAYER_CAP = 20


def _usage_q(payers: int, calls: int, rec_days: Optional[int]) -> int:
    """[MR-2] Usage quality with unique payers dominant.

    payers×5 + calls-capped-at-payers×20 + recency bonus. Raw call volume
    can't buy rank: a wallet pumping calls through one payer caps out, while
    every additional distinct payer is worth 5. Zero payers = zero volume
    credit (wash traffic with no distinct buyers scores only recency).
    """
    q = payers * 5 + min(calls, payers * CALLS_PER_PAYER_CAP)
    if rec_days is not None and rec_days <= 7:
        q += 5
    return q


def _delivery_why(row: dict) -> str:
    """One human line from a service_scores row (the Prober's public output).
    [MR-3]: the MPP/Tempo label rides along — known from free T0 probes, so it
    can appear even when a service has no paid probes yet."""
    parts: list[str] = []
    if FLAG_NO_DELIVERY in (row.get("flags") or []):
        failed_at = str(row.get("last_fail_at") or "")[:10]
        parts.append("⚠ took payment without delivering"
                     + (f" on {failed_at}" if failed_at else ""))
    else:
        n = row.get("paid_probes") or 0
        rate = row.get("delivery_rate")
        if n and rate is not None:
            pct = f"{float(rate) * 100:.0f}%"
            p50 = row.get("latency_p50_ms")
            lat = f", median {int(p50)}ms" if isinstance(p50, (int, float)) else ""
            parts.append(f"probed {n}× in {row.get('window_days', 30)}d, "
                         f"{pct} delivered{lat}")
    if row.get("mpp_option"):
        parts.append("also payable via MPP/Tempo")
    if row.get("usdg_option"):
        parts.append("also payable in USDG on Robinhood Chain")
    return " · ".join(parts)


def decide(cands: list[dict], remaining: Decimal,
           usage_aware: bool = False,
           scores: Optional[dict] = None) -> tuple[list[dict], Optional[dict]]:
    """Filter + rank. Returns (scored_with_verdicts, recommendation). Pure.

    Stages: junk-filter (no schema = stub; factory fingerprint) → budget gate →
    usage-quality score ([MR-2]: unique payers dominant — payers×5 + capped
    calls + recency, factory downrank) → delivery factor (×score row from the
    Prober, join on resource URL) → sort by quality desc, price asc.

    `usage_aware` (verified_route uses True): a wallet with many listings is only
    a "factory" if those listings are MOSTLY UNPROVEN. A trustworthy multi-product
    provider (e.g. CMC) whose endpoints each have real payers is NOT a factory and
    is never downranked for breadth. Known-trusted payTo addresses are always
    exempt; known factory prefixes are always factories. Default False preserves
    the legacy count-only behavior the Arbitrum radar + its tests rely on.

    `scores` (PROBER_SPEC): {resource_url: service_scores row}. Unprobed
    services get factor 1.0 (never punish absence of data); a row carrying
    took_payment_no_delivery is HARD-DROPPED from the recommendation while
    staying listed + flagged. The function stays pure — callers fetch the
    dict (gateway: services.supabase.fetch_service_scores)."""
    scores = scores or {}
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

        # Stage 3 — usage quality ([MR-2] payers dominant)
        rec_days = _recency_days(c["last_called"])
        q = _usage_q(c["payers30d"], c["calls30d"], rec_days)
        if is_factory:
            q = q // 4
        if c["payers30d"] == 0 and c["calls30d"] == 0:
            flags.append("unproven(0/0)")
        if c["calls30d"] > max(c["payers30d"], 1) * CALLS_PER_PAYER_CAP:
            flags.append("single_payer_volume")

        # Stage 4 — delivery factor (the Prober's axis; unprobed = neutral)
        score_row = scores.get(c["url"]) or {}
        try:
            factor = float(score_row.get("delivery_factor", 1.0) or 1.0)
        except (TypeError, ValueError):
            factor = 1.0
        if factor != 1.0:
            q = int(q * factor)
        for f in (score_row.get("flags") or []):
            if f in _NO_DELIVERY_FLAGS:
                flags.append(f)
        delivery_why = _delivery_why(score_row) if score_row else ""

        scored.append({**c, "flags": flags, "dropped": dropped,
                       "drop_reason": reason, "quality": q, "rec_days": rec_days,
                       "delivery": ({
                           "factor": factor,
                           "rate": score_row.get("delivery_rate"),
                           "paid_probes": score_row.get("paid_probes"),
                           "latency_p50_ms": score_row.get("latency_p50_ms"),
                       } if score_row else None),
                       "mpp_option": bool(score_row.get("mpp_option")),
                       "usdg_option": bool(score_row.get("usdg_option")),
                       "why": delivery_why})

    survivors = [s for s in scored if not s["dropped"]]
    survivors.sort(key=lambda s: (-s["quality"], s["price_usd"]))
    # A no-delivery flag (confirmed OR unconfirmed) is listed but NEVER
    # recommended — protection doesn't wait for confirmation.
    recommendation = next(
        (s for s in survivors if not _NO_DELIVERY_FLAGS & set(s["flags"])), None)
    return scored, recommendation


def rank_from_payload(data: dict, need: str, budget: Decimal,
                      chain: Optional[str] = None,
                      extra: Optional[Iterable[dict]] = None,
                      scores: Optional[dict] = None) -> dict:
    """Assemble a JSON-able Radar result from an already-fetched Bazaar payload.

    Pure (no I/O) so the async gateway can fetch with httpx and hand the payload
    here. `extra` lets the Robinhood crawler (Day 2b) inject candidates Bazaar
    can't see; they flow through the same chain filter + ranking. `scores` is
    the Prober's service_scores dict (see decide()).
    """
    cands = filter_chain(parse_resources(data), chain)
    if extra:
        cands = cands + filter_chain(list(extra), chain)
    scored, rec = decide(cands, budget, scores=scores)
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
         fetch: Callable[[str], dict] = _default_get,
         scores: Optional[dict] = None) -> dict:
    """End-to-end (sync): fetch Bazaar → rank_from_payload. Used by the CLI path.

    The async gateway path calls `rank_from_payload` directly with an httpx fetch.
    """
    return rank_from_payload(fetch_bazaar(need, fetch=fetch), need, budget, chain,
                             scores=scores)


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
    if s.get("relevance"):
        # AGE-43: which of the buyer's need concepts this listing matched.
        out["matches_need"] = s.get("relevance_matched") or []
    if s.get("why"):
        out["why"] = s["why"]
    if s.get("delivery"):
        out["delivery"] = s["delivery"]
    if s.get("mpp_option"):
        out["mpp_option"] = True   # [MR-3] label only — never settled by us
    if s.get("usdg_option"):
        out["usdg_option"] = True  # AGE-18 label only — never settled by us
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
    # AgentPay's own gateway wallet: multiple legitimate tools under one
    # wallet (session_create / pre_trade_check / verified_route). Trusted =
    # exempt from factory-flagging/sybil-collapse ONLY — no rank boost, and
    # the Prober deliberately never delivery-scores our own tools (factor
    # stays neutral 1.0). Fair ranking, no self-dealing.
    "0xe8b25a72dd6aef69515452a61ad231c7df2843b7",          # AgentPay gateway (Base)
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


# ── Relevance tier (AGE-43) ───────────────────────────────────────────────────
# The catalog-wide usage rank answered "what's most used, period" — an email
# tool with 873 payers won "dex pair liquidity". verified_route now recommends
# within the NEED-RELEVANT tier first (usage × delivery still ranks inside it)
# and falls back to catalog-wide only when nothing matches, saying so.

# Generic terms that appear in most listings and carry no topical signal.
_RELEVANCE_STOPWORDS = {
    "the", "a", "an", "for", "and", "or", "of", "to", "in", "on", "with",
    "get", "via", "per", "any", "all", "best", "real", "top",
    "api", "data", "service", "tool", "tools", "x402", "agent", "agents",
}

# Small curated synonym map (domain-specific, deliberately tight — this is a
# match-widener, not NLP). Keys and values are lowercase stems; a need token
# expands to itself + its synonyms.
_RELEVANCE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "dex":       ("swap", "amm", "pool", "pancakeswap", "uniswap"),
    "liquidity": ("pool", "tvl", "depth", "pair"),
    "pair":      ("pool",),   # NOT "market" — matches half the catalog's tags
    "price":     ("quote", "prices", "ticker", "rate"),
    "news":      ("headline", "headlines", "articles"),
    "security":  ("honeypot", "audit", "scam", "rug"),
    "wallet":    ("balance", "address", "account"),
    "email":     ("mail", "mailbox", "inbox", "imap"),
    "search":    ("lookup", "find", "query"),
    "image":     ("photo", "picture", "img", "vision"),
    "llm":       ("inference", "chat", "completion", "completions", "model"),
    "gas":       ("fees", "gwei"),
    "nft":       ("collectible", "collectibles", "opensea"),
    "weather":   ("forecast", "temperature"),
}


def _need_tokens(need: str) -> list[str]:
    """Topical tokens from a need string: lowercased, stopword-filtered, len>2."""
    toks = re.findall(r"[a-z0-9]+", (need or "").lower())
    return list(dict.fromkeys(
        t for t in toks if len(t) > 2 and t not in _RELEVANCE_STOPWORDS))


def _relevance(cand: dict, tokens: list[str]) -> tuple[int, list[str]]:
    """(matched-token count, matched tokens) for a candidate vs need tokens.

    A token matches on a WORD-BOUNDARY PREFIX of the candidate's searchable
    text (name, url, tags, description): "dex" matches "dexscreener" and
    "dex-pairs" but not "index" or "codex". Synonyms count for their source
    token (matching "swap" credits "dex"), so multi-word needs rank by how
    many of the buyer's own concepts a listing covers — not how often.
    """
    text = " ".join([
        cand.get("name") or "", cand.get("url") or "",
        " ".join(cand.get("tags") or []), cand.get("description") or "",
    ]).lower()
    matched: list[str] = []
    for tok in tokens:
        for term in (tok, *_RELEVANCE_SYNONYMS.get(tok, ())):
            if re.search(r"\b" + re.escape(term), text):
                matched.append(tok)
                break
    return len(matched), matched


def _ready_to_pay(s: Optional[dict]) -> Optional[dict]:
    """The buyer-facing 'how to pay this' block for the recommendation."""
    if not s:
        return None
    return {"url": s["url"], "network": s["network_caip2"] or s["network"],
            "price_usd": (str(s["price_usd"]) if s["price_usd"] is not None else None),
            "accepts": s.get("accepts") or {}}


def verified_route_from_payloads(payloads: list[dict], need: str, budget: Decimal,
                                 chain: Optional[str] = None,
                                 extra: Optional[Iterable[dict]] = None,
                                 scores: Optional[dict] = None) -> dict:
    """Assemble the paid verified_route result from swept Bazaar payloads. Pure.

    DISCOVER (merge+dedup many queries) → FILTER (chain) → DECIDE (junk/factory/
    rank over the FULL set, delivery factor joined from `scores`) → COLLAPSE
    (sybil clusters → one entry) → RELEVANCE tier (AGE-43: need-matching
    providers outrank the catalog; usage × delivery ranks within the tier;
    empty tier falls back catalog-wide with `relevance_fallback`) →
    recommend (never a service flagged took_payment_no_delivery — listed,
    not recommended).
    """
    merged = merge_resources(payloads)
    cands = filter_chain(parse_resources(merged), chain)
    if extra:
        cands = cands + filter_chain(list(extra), chain)

    scored, _ = decide(cands, budget, usage_aware=True, scores=scores)
    survivors = [s for s in scored if not s["dropped"]]
    kept, stats = collapse_sybils(survivors)

    # ── Relevance tier (AGE-43) ────────────────────────────────────────────
    tokens = _need_tokens(need)
    for s in kept:
        s["relevance"], s["relevance_matched"] = _relevance(s, tokens)
    # Relevant tier first; more of the buyer's concepts covered beats fewer;
    # usage quality (already delivery-factored) then price rank within.
    # collapse_sybils preserved decide()'s (quality desc, price asc) order,
    # so this stable sort is a re-tier of that ranking, not a re-rank.
    kept.sort(key=lambda s: (0 if s["relevance"] > 0 else 1, -s["relevance"]))
    relevant = [s for s in kept if s["relevance"] > 0]
    fallback = bool(tokens) and not relevant
    pool = relevant if relevant else kept
    rec = next((s for s in pool if not _NO_DELIVERY_FLAGS & set(s["flags"])), None)

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
            "need_relevant": len(relevant),
            "unique_wallets": stats["unique_wallets"],
            "sybil_collapsed": stats["sybil_collapsed"],
            "biggest_factory": stats["biggest_factory"],
        },
        **({"relevance_fallback": True} if fallback else {}),
        "vetting": (
            f"swept {len(payloads)} queries → {len(cands)} listings → "
            f"collapsed {stats['sybil_collapsed']} sybil listings → "
            f"{len(kept)} real providers"
            + (f" → {len(relevant)} match the need" if relevant else (
                " → none match the need — recommending catalog-wide by usage"
                if fallback else ""))
        ),
    }
