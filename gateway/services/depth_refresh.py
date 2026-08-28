"""
depth_refresh.py — the gateway refreshes provider_depth ITSELF (AGE-138).

The weekly `tools/payer_depth.py` run started as a laptop step because the
extraction once needed a Dune key. It doesn't anymore: the x402scan public
tRPC API is keyless, and the gateway already holds the Supabase write key —
so there is no reason a human should run anything. This loop checks daily
whether the freshest provider_depth row is older than REFRESH_AFTER_DAYS and,
when it is, re-pulls the payer shape for the payTos we actually rank (the
provider_map ∪ the market head) and upserts the rows. radar.decide() ignores
rows older than 7 days, so the refresh-at-5-days cadence keeps ranking inside
the freshness window with two days of slack for outages.

Same math as tools/payer_depth.py (which stays for ad-hoc runs and the full
calibration report): per (recipient, payer) legs from `public.transfers.list`
(newest first, sampled at MAX_LEGS for mega-sellers and marked `sampled`),
market-wide fanout from `public.buyers.all.list`, weight = 1.0 for a returning
payer else 0.2 + 0.8·(1 − fanout). Aggregates only — no payer wallet is
stored. Be a polite guest on x402scan: one request every THROTTLE_S seconds,
identified UA, bounded payTo set; a full refresh is ~300 requests spread over
~10 minutes of background time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx

from gateway.config import settings
from gateway.services import supabase as sb

logger = logging.getLogger(__name__)

X402SCAN_TRPC = "https://www.x402scan.com/api/trpc"
UA = "agentpay-gateway/depth-refresh (+https://agentpay.tools)"
CHAIN = "base"
NETWORK = "eip155:8453"

THROTTLE_S = 0.4
PAGE = 500
MAX_LEGS = 3000            # per payTo; a 7.5M-leg fleet is SAMPLED (newest first)
MAX_PAYTOS = 200           # provider_map ∪ market head, bounded
TOP_HEAD = 25              # top sellers by tx_count added so the head stays covered
CHECK_INTERVAL_S = 24 * 3600
FIRST_CHECK_DELAY_S = 300  # let the gateway settle before the first check
REFRESH_AFTER_DAYS = 5     # newest row older than this → refresh (ranking cutoff is 7)


async def _rpc(client: httpx.AsyncClient, proc: str, inp: dict) -> dict:
    q = urllib.parse.quote(json.dumps({"json": inp}))
    for attempt in range(3):
        await asyncio.sleep(THROTTLE_S)
        try:
            r = await client.get(f"{X402SCAN_TRPC}/{proc}?input={q}",
                                 headers={"User-Agent": UA, "Accept": "application/json"})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["result"]["data"]["json"]
        except (httpx.HTTPError, KeyError, ValueError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


async def _top_sellers(client: httpx.AsyncClient, n: int) -> list[str]:
    res = await _rpc(client, "public.sellers.all.list",
                     {"timeframe": 30, "chain": CHAIN,
                      "sorting": {"id": "tx_count", "desc": True},
                      "pagination": {"page": 0, "page_size": n}})
    return [str(it.get("recipient") or "").lower() for it in (res.get("items") or [])]


async def _seller_totals(client: httpx.AsyncClient, paytos: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(paytos), 50):
        res = await _rpc(client, "public.sellers.all.list",
                         {"timeframe": 30, "chain": CHAIN,
                          "recipients": {"include": paytos[i:i + 50]},
                          "pagination": {"page": 0, "page_size": PAGE}})
        for it in res.get("items") or []:
            out[str(it.get("recipient") or "").lower()] = it
    return out


async def _legs(client: httpx.AsyncClient, payto: str) -> tuple[list[dict], bool]:
    legs: list[dict] = []
    page = 0
    while True:
        res = await _rpc(client, "public.transfers.list",
                         {"timeframe": 30, "chain": CHAIN,
                          "recipients": {"include": [payto]},
                          "sorting": {"id": "block_timestamp", "desc": True},
                          "pagination": {"page": page, "page_size": PAGE}})
        legs.extend(res.get("items") or [])
        if not res.get("hasNextPage"):
            return legs, False
        if len(legs) >= MAX_LEGS:
            return legs, True
        page += 1


async def _buyer_shape(client: httpx.AsyncClient, senders: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(senders), 50):
        res = await _rpc(client, "public.buyers.all.list",
                         {"timeframe": 30, "chain": CHAIN,
                          "senders": {"include": senders[i:i + 50]},
                          "pagination": {"page": 0, "page_size": PAGE}})
        for it in res.get("items") or []:
            out[str(it.get("sender") or "").lower()] = it
    return out


# ── pure math (mirrors tools/payer_depth.py; tested against the same numbers) ─

def payer_weight(legs_here: int, fanout: float) -> float:
    if legs_here >= 2:
        return 1.0
    return 0.2 + 0.8 * (1.0 - min(max(fanout, 0.0), 1.0))


def _p50(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def depth_row(payto: str, pairs: dict[str, dict], shape: dict[str, dict],
              total: Optional[dict], sampled: bool, now_iso: str) -> dict:
    """One provider_depth row from per-payer aggregates. Pure.

    pairs: {sender: {legs, usd, first, last}}; shape: {sender: {tx_count,
    unique_sellers}} market-wide; total: the sellers-MV row (true 30d totals)."""
    weights = []
    for snd, p in pairs.items():
        s = shape.get(snd) or {}
        txc = int(s.get("tx_count") or 0) or p["legs"]
        fan = (int(s.get("unique_sellers") or 0) or 1) / max(txc, 1)
        weights.append(payer_weight(p["legs"], fan))
    payers = len(pairs)
    legs_n = sum(p["legs"] for p in pairs.values())
    usd = sum(p["usd"] for p in pairs.values())
    returning = sum(1 for p in pairs.values() if p["legs"] >= 2)
    total = total or {}
    return {
        "pay_to": payto, "network": NETWORK, "window_days": 30,
        "payers": payers, "legs": legs_n, "usd": round(usd, 6),
        "mean_leg": round(usd / max(legs_n, 1), 6),
        "returning_payers": returning,
        "retention": round(returning / max(payers, 1), 4),
        "effective_payers": round(sum(weights), 3),
        "payer_quality": round(sum(weights) / max(payers, 1), 4),
        "prober_share": round(sum(1 for w in weights if w < 0.5) / max(payers, 1), 4),
        "p50_legs_per_payer": round(_p50([p["legs"] for p in pairs.values()]), 2),
        "top_payer_share": round(max(p["usd"] for p in pairs.values()) / max(usd, 1e-9), 4)
                           if pairs else 0.0,
        "first_leg_at": min(p["first"] for p in pairs.values()) if pairs else None,
        "last_leg_at": max(p["last"] for p in pairs.values()) if pairs else None,
        "sampled": sampled,
        "total_legs_30d": int(total.get("tx_count") or 0) or legs_n,
        "total_payers_30d": int(total.get("unique_buyers") or 0) or payers,
        "source": "gateway", "updated_at": now_iso,
    }


def pairs_from_legs(legs: list[dict]) -> dict[str, dict]:
    """Per-sender aggregates from raw transfer rows. Pure."""
    pairs: dict[str, dict] = {}
    for lg in legs:
        snd = str(lg.get("sender") or "").lower()
        if not snd:
            continue
        try:
            usd = float(lg.get("amount") or 0) / (10 ** int(lg.get("decimals") or 6))
        except (TypeError, ValueError):
            usd = 0.0
        ts = str(lg.get("block_timestamp") or "")
        p = pairs.setdefault(snd, {"legs": 0, "usd": 0.0, "first": ts, "last": ts})
        p["legs"] += 1
        p["usd"] += usd
        p["first"] = min(p["first"], ts)
        p["last"] = max(p["last"], ts)
    return pairs


# ── orchestration ────────────────────────────────────────────────────────────

def _newest_row_age_days(depth: dict[str, dict]) -> Optional[float]:
    from gateway.radar import _recency_days
    ages = [a for a in (_recency_days(str(r.get("updated_at") or "")) for r in depth.values())
            if a is not None]
    return min(ages) if ages else None


async def _payto_set(client: httpx.AsyncClient) -> list[str]:
    """provider_map payTos (Base) ∪ our own ∪ the market head, EVM only."""
    from gateway.radar import KNOWN_TRUSTED
    paytos = {pt for (pt, net) in (await sb.fetch_provider_map()).keys() if net == NETWORK}
    paytos |= {t for t in KNOWN_TRUSTED if t.startswith("0x") and len(t) == 42}
    try:
        paytos |= set(await _top_sellers(client, TOP_HEAD))
    except Exception as e:
        logger.warning(f"[DEPTH] top-sellers fetch failed ({e}) — refreshing map payTos only")
    paytos = sorted(p for p in paytos if p.startswith("0x") and len(p) == 42)
    return paytos[:MAX_PAYTOS]


async def refresh(limit: Optional[int] = None) -> int:
    """One full refresh: pull, compute, upsert. Returns rows written."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        paytos = await _payto_set(client)
        if limit:
            paytos = paytos[:limit]
        if not paytos:
            logger.info("[DEPTH] no payTos to refresh (provider_map empty and head unreachable)")
            return 0
        try:
            totals = await _seller_totals(client, paytos)
        except Exception as e:
            logger.warning(f"[DEPTH] seller-totals fetch failed ({e}) — "
                           "true 30d totals fall back to the sample")
            totals = {}
        now_iso = datetime.now(timezone.utc).isoformat()
        rows: list[dict] = []
        all_pairs: dict[str, dict[str, dict]] = {}
        sampled_by: dict[str, bool] = {}
        senders: set[str] = set()
        for pt in paytos:
            try:
                legs, sampled = await _legs(client, pt)
            except Exception as e:
                logger.warning(f"[DEPTH] legs fetch failed for {pt[:10]}… ({e}) — skipped")
                continue
            if not legs:
                continue
            pairs = pairs_from_legs(legs)
            all_pairs[pt] = pairs
            sampled_by[pt] = sampled
            senders.update(pairs)
        shape: dict[str, dict] = {}
        try:
            shape = await _buyer_shape(client, sorted(senders))
        except Exception as e:
            logger.warning(f"[DEPTH] buyer-shape fetch failed ({e}) — "
                           "fanout falls back to per-seller legs")
        for pt, pairs in all_pairs.items():
            rows.append(depth_row(pt, pairs, shape, totals.get(pt), sampled_by[pt], now_iso))
    if not rows:
        return 0
    written = await sb.upsert_provider_depth(rows)
    if written:
        sb._depth_cache_clear()
        logger.info(f"[DEPTH] refreshed {written} provider_depth rows "
                    f"({len(paytos)} payTos scanned)")
    return written


async def refresh_loop() -> None:
    """Daily check; refresh when the newest row is > REFRESH_AFTER_DAYS old.
    Register in main.py's lifespan. A manual tools/payer_depth.py --write run
    resets the clock (the check reads updated_at, not who wrote it)."""
    await asyncio.sleep(FIRST_CHECK_DELAY_S + random.uniform(0, 60))
    while True:
        try:
            depth = await sb.fetch_provider_depth(force=True)
            age = _newest_row_age_days(depth)
            if age is None or age >= REFRESH_AFTER_DAYS:
                logger.info(f"[DEPTH] newest row age={age} — refreshing")
                await refresh()
            else:
                logger.debug(f"[DEPTH] fresh (age {age}d) — next check in 24h")
        except Exception as e:  # pragma: no cover — the loop must survive anything
            logger.error(f"depth refresh loop error: {e}")
        await asyncio.sleep(CHECK_INTERVAL_S)
