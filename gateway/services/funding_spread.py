"""funding_spread — cross-venue perp funding divergence for one asset.

Nine venues are read in parallel; the ones that answer form the spread.
Rates are normalised to a per-8h figure so hourly venues (Hyperliquid, dYdX)
compare with 8h ones. The 7-day percentile puts "wide" in context: each
venue's history is averaged into 8h buckets and the cross-venue spread is
computed per bucket. Binance and Bybit refuse US egress; they join the
spread automatically from any other region. Thresholds and the fee
assumption are in docs/funding-spread.md.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

EIGHT_H_MS = 8 * 3600 * 1000
HISTORY_DAYS = 7
TAKER_FEE_PCT = 0.05          # per leg, per side — a round trip on two venues is 4 legs
VENUES = ("binance", "bybit", "okx", "gate", "bitget", "hyperliquid", "dydx", "mexc", "kucoin")


@dataclass
class VenueFunding:
    venue: str
    rate_pct: float                       # per funding interval, in percent
    interval_hours: float
    next_funding_time: Optional[int]      # ms epoch
    history: list[tuple[int, float]] = field(default_factory=list)   # (ms, rate_pct per interval)

    @property
    def rate_8h_pct(self) -> float:
        return self.rate_pct * 8.0 / self.interval_hours

    def history_8h(self) -> list[tuple[int, float]]:
        k = 8.0 / self.interval_hours
        return [(t, r * k) for t, r in self.history]


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ── Venue readers ────────────────────────────────────────────────────────────

async def _binance(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    sym = f"{asset}USDT"
    r = await c.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": sym})
    r.raise_for_status()
    d = r.json()
    interval = 8.0
    try:
        fi = await c.get("https://fapi.binance.com/fapi/v1/fundingInfo")
        for row in fi.json() if fi.status_code == 200 else []:
            if row.get("symbol") == sym and row.get("fundingIntervalHours"):
                interval = float(row["fundingIntervalHours"])
    except Exception:
        pass
    hist: list[tuple[int, float]] = []
    try:
        h = await c.get("https://fapi.binance.com/fapi/v1/fundingRate",
                        params={"symbol": sym, "startTime": since_ms, "limit": 1000})
        hist = [(int(x["fundingTime"]), _f(x["fundingRate"]) * 100) for x in h.json()]
    except Exception:
        pass
    return VenueFunding("binance", _f(d.get("lastFundingRate")) * 100, interval,
                        int(d.get("nextFundingTime") or 0) or None, hist)


async def _bybit(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    sym = f"{asset}USDT"
    r = await c.get("https://api.bybit.com/v5/market/tickers",
                    params={"category": "linear", "symbol": sym})
    r.raise_for_status()
    rows = r.json().get("result", {}).get("list") or []
    if not rows:
        raise ValueError("no such contract")
    d = rows[0]
    interval = 8.0
    try:
        ii = await c.get("https://api.bybit.com/v5/market/instruments-info",
                         params={"category": "linear", "symbol": sym})
        mins = (ii.json().get("result", {}).get("list") or [{}])[0].get("fundingInterval")
        if mins:
            interval = float(mins) / 60.0
    except Exception:
        pass
    hist: list[tuple[int, float]] = []
    try:
        h = await c.get("https://api.bybit.com/v5/market/funding/history",
                        params={"category": "linear", "symbol": sym, "limit": 200})
        hist = [(int(x["fundingRateTimestamp"]), _f(x["fundingRate"]) * 100)
                for x in h.json().get("result", {}).get("list") or []
                if int(x["fundingRateTimestamp"]) >= since_ms]
    except Exception:
        pass
    return VenueFunding("bybit", _f(d.get("fundingRate")) * 100, interval,
                        int(d.get("nextFundingTime") or 0) or None, hist)


async def _okx(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    inst = f"{asset}-USDT-SWAP"
    r = await c.get("https://www.okx.com/api/v5/public/funding-rate", params={"instId": inst})
    r.raise_for_status()
    rows = r.json().get("data") or []
    if not rows:
        raise ValueError("no such contract")
    d = rows[0]
    t0, t1 = int(d.get("fundingTime") or 0), int(d.get("nextFundingTime") or 0)
    interval = (t1 - t0) / 3.6e6 if t1 > t0 else 8.0
    hist: list[tuple[int, float]] = []
    try:
        h = await c.get("https://www.okx.com/api/v5/public/funding-rate-history",
                        params={"instId": inst, "limit": 100})
        hist = [(int(x["fundingTime"]), _f(x.get("realizedRate", x.get("fundingRate"))) * 100)
                for x in h.json().get("data") or [] if int(x["fundingTime"]) >= since_ms]
    except Exception:
        pass
    return VenueFunding("okx", _f(d.get("fundingRate")) * 100, interval, t1 or None, hist)


async def _gate(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    name = f"{asset}_USDT"
    r = await c.get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{name}")
    r.raise_for_status()
    d = r.json()
    interval = _f(d.get("funding_interval"), 28800) / 3600.0
    hist: list[tuple[int, float]] = []
    try:
        h = await c.get("https://api.gateio.ws/api/v4/futures/usdt/funding_rate",
                        params={"contract": name, "limit": 100})
        hist = [(int(x["t"]) * 1000, _f(x["r"]) * 100) for x in h.json()
                if int(x["t"]) * 1000 >= since_ms]
    except Exception:
        pass
    nxt = int(d.get("funding_next_apply") or 0) * 1000
    return VenueFunding("gate", _f(d.get("funding_rate")) * 100, interval, nxt or None, hist)


async def _bitget(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    p = {"symbol": f"{asset}USDT", "productType": "USDT-FUTURES"}
    r = await c.get("https://api.bitget.com/api/v2/mix/market/current-fund-rate", params=p)
    r.raise_for_status()
    rows = r.json().get("data") or []
    if not rows:
        raise ValueError("no such contract")
    d = rows[0]
    interval = _f(d.get("fundingRateInterval"), 8) or 8.0
    hist: list[tuple[int, float]] = []
    try:
        h = await c.get("https://api.bitget.com/api/v2/mix/market/history-fund-rate",
                        params={**p, "pageSize": 100})
        hist = [(int(x["fundingTime"]), _f(x["fundingRate"]) * 100)
                for x in h.json().get("data") or [] if int(x["fundingTime"]) >= since_ms]
    except Exception:
        pass
    return VenueFunding("bitget", _f(d.get("fundingRate")) * 100, interval,
                        int(d.get("nextUpdate") or 0) or None, hist)


async def _hyperliquid(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    base = "https://api.hyperliquid.xyz/info"
    r = await c.post(base, json={"type": "metaAndAssetCtxs"})
    r.raise_for_status()
    meta, ctxs = r.json()
    idx = next((i for i, u in enumerate(meta.get("universe", [])) if u.get("name") == asset), None)
    if idx is None or idx >= len(ctxs):
        raise ValueError("no such contract")
    hist: list[tuple[int, float]] = []
    try:
        h = await c.post(base, json={"type": "fundingHistory", "coin": asset, "startTime": since_ms})
        hist = [(int(x["time"]), _f(x["fundingRate"]) * 100) for x in h.json()]
    except Exception:
        pass
    now = int(time.time() * 1000)
    nxt = (now // 3_600_000 + 1) * 3_600_000          # hourly, on the hour
    return VenueFunding("hyperliquid", _f(ctxs[idx].get("funding")) * 100, 1.0, nxt, hist)


async def _dydx(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    ticker = f"{asset}-USD"
    r = await c.get("https://indexer.dydx.trade/v4/perpetualMarkets", params={"ticker": ticker})
    r.raise_for_status()
    m = (r.json().get("markets") or {}).get(ticker)
    if not m:
        raise ValueError("no such contract")
    hist: list[tuple[int, float]] = []
    try:
        h = await c.get(f"https://indexer.dydx.trade/v4/historicalFunding/{ticker}",
                        params={"limit": 200})
        for x in h.json().get("historicalFunding") or []:
            ts = int(datetime.fromisoformat(x["effectiveAt"].replace("Z", "+00:00")).timestamp() * 1000)
            if ts >= since_ms:
                hist.append((ts, _f(x["rate"]) * 100))
    except Exception:
        pass
    now = int(time.time() * 1000)
    nxt = (now // 3_600_000 + 1) * 3_600_000
    return VenueFunding("dydx", _f(m.get("nextFundingRate")) * 100, 1.0, nxt, hist)


async def _mexc(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    sym = f"{asset}_USDT"
    r = await c.get(f"https://contract.mexc.com/api/v1/contract/funding_rate/{sym}")
    r.raise_for_status()
    d = r.json().get("data") or {}
    if not d:
        raise ValueError("no such contract")
    hist: list[tuple[int, float]] = []
    try:
        h = await c.get("https://contract.mexc.com/api/v1/contract/funding_rate/history",
                        params={"symbol": sym, "page_num": 1, "page_size": 100})
        hist = [(int(x["settleTime"]), _f(x["fundingRate"]) * 100)
                for x in (h.json().get("data") or {}).get("resultList") or []
                if int(x["settleTime"]) >= since_ms]
    except Exception:
        pass
    return VenueFunding("mexc", _f(d.get("fundingRate")) * 100, _f(d.get("collectCycle"), 8) or 8.0,
                        int(d.get("nextSettleTime") or 0) or None, hist)


async def _kucoin(c: httpx.AsyncClient, asset: str, since_ms: int) -> VenueFunding:
    sym = f"{'XBT' if asset == 'BTC' else asset}USDTM"
    r = await c.get(f"https://api-futures.kucoin.com/api/v1/funding-rate/{sym}/current")
    r.raise_for_status()
    d = r.json().get("data") or {}
    if not d:
        raise ValueError("no such contract")
    interval = _f(d.get("granularity"), 28800000) / 3.6e6
    hist: list[tuple[int, float]] = []
    try:
        h = await c.get("https://api-futures.kucoin.com/api/v1/contract/funding-rates",
                        params={"symbol": sym, "from": since_ms, "to": int(time.time() * 1000)})
        hist = [(int(x["timepoint"]), _f(x["fundingRate"]) * 100) for x in h.json().get("data") or []]
    except Exception:
        pass
    return VenueFunding("kucoin", _f(d.get("value")) * 100, interval,
                        int(d.get("fundingTime") or 0) or None, hist)


_READERS: dict[str, Callable] = {
    "binance": _binance, "bybit": _bybit, "okx": _okx, "gate": _gate, "bitget": _bitget,
    "hyperliquid": _hyperliquid, "dydx": _dydx, "mexc": _mexc, "kucoin": _kucoin,
}


# ── Spread maths ─────────────────────────────────────────────────────────────

def _percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


def spread_history(venues: list[VenueFunding], now_ms: int, days: int = HISTORY_DAYS) -> dict:
    """Cross-venue spread per 8h bucket over the window; buckets with one venue are skipped."""
    first = now_ms // EIGHT_H_MS - days * 3
    per_bucket: dict[int, dict[str, list[float]]] = {}
    for v in venues:
        for ts, r8 in v.history_8h():
            b = ts // EIGHT_H_MS
            if b < first:
                continue
            per_bucket.setdefault(b, {}).setdefault(v.venue, []).append(r8)
    spreads: list[tuple[int, float]] = []
    for b in sorted(per_bucket):
        means = [statistics.fmean(x) for x in per_bucket[b].values()]
        if len(means) >= 2:
            spreads.append((b, max(means) - min(means)))
    vals = [s for _, s in spreads]
    return {
        "days": days,
        "buckets": len(vals),
        "venues_with_history": sorted({v.venue for v in venues if v.history}),
        "spread_pct_8h_p50": round(_percentile(vals, 50), 5) if vals else None,
        "spread_pct_8h_p90": round(_percentile(vals, 90), 5) if vals else None,
        "spread_pct_8h_max": round(max(vals), 5) if vals else None,
        "_values": vals,
    }


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def build_response(asset: str, venues: list[VenueFunding], failed: dict[str, str],
                   notional_usd: Optional[float], now_ms: int) -> dict:
    if len(venues) < 2:
        return {"error": f"funding for {asset} found on {len(venues)} venue(s); a spread needs two",
                "venues_unavailable": failed}
    rows = sorted(venues, key=lambda v: v.rate_8h_pct)
    cheapest, dearest = rows[0], rows[-1]
    r8 = [v.rate_8h_pct for v in rows]
    median = statistics.median(r8)
    spread = dearest.rate_8h_pct - cheapest.rate_8h_pct

    hist = spread_history(venues, now_ms)
    vals = hist.pop("_values")
    pct = round(100 * sum(1 for s in vals if s <= spread) / len(vals)) if vals else None
    hist["current_percentile"] = pct

    read = (f"{asset} funding {dearest.rate_8h_pct:.4f}%/8h on {dearest.venue} vs "
            f"{cheapest.rate_8h_pct:.4f}% on {cheapest.venue} — {spread * 100:.1f} bps/8h")
    read += f", {_ordinal(pct)} percentile of the last {hist['days']} days" if pct is not None else ""
    read += f" ({len(venues)} of {len(VENUES)} venues answered)"

    carry = None
    if notional_usd and notional_usd > 0:
        per_8h = spread / 100 * notional_usd
        fees = TAKER_FEE_PCT / 100 * notional_usd * 4
        carry = {
            "notional_usd": notional_usd,
            "long_on": cheapest.venue, "short_on": dearest.venue,
            "per_8h_usd": round(per_8h, 4),
            "per_day_usd": round(per_8h * 3, 4),
            "per_30d_usd": round(per_8h * 90, 2),
            "taker_fees_round_trip_usd": round(fees, 4),
            "breakeven_hours": round(fees / per_8h * 8, 1) if per_8h > 0 else None,
            "assumptions": f"taker {TAKER_FEE_PCT}% per leg on both venues, open and close; "
                           "spread held constant; no slippage, no basis move",
        }

    return {
        "asset": asset,
        "as_of": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds"),
        "venues": [{
            "venue": v.venue,
            "rate_8h_pct": round(v.rate_8h_pct, 6),
            "rate_annualized_pct": round(v.rate_8h_pct * 3 * 365, 2),
            "raw_rate_pct": round(v.rate_pct, 6),
            "interval_hours": v.interval_hours,
            "next_funding_time": v.next_funding_time,
        } for v in rows],
        "spread": {
            "pct_8h": round(spread, 6),
            "bps_8h": round(spread * 100, 2),
            "annualized_pct": round(spread * 3 * 365, 2),
            "median_8h_pct": round(median, 6),
            "max_deviation_from_median_pct": round(max(abs(x - median) for x in r8), 6),
            "cheapest": cheapest.venue,
            "dearest": dearest.venue,
        },
        "history": hist,
        "carry_estimate": carry,
        "read": read,
        "venues_unavailable": failed,
    }


async def fetch(client: httpx.AsyncClient, params: dict, *,
                now_ms: Optional[int] = None) -> dict:
    asset = str(params.get("symbol") or params.get("asset") or "BTC").strip().upper()
    asset = asset.replace("USDT", "").replace("-PERP", "") or "BTC"
    notional = params.get("notional_usd")
    notional = _f(notional) if notional is not None else None
    now_ms = now_ms or int(time.time() * 1000)
    since_ms = now_ms - HISTORY_DAYS * 24 * 3600 * 1000

    async def one(name: str):
        try:
            return await asyncio.wait_for(_READERS[name](client, asset, since_ms), timeout=12.0)
        except Exception as e:                      # one venue down never fails the call
            return (name, _short_reason(e))

    results = await asyncio.gather(*(one(n) for n in VENUES))
    venues = [r for r in results if isinstance(r, VenueFunding)]
    failed = {n: why for n, why in (r for r in results if isinstance(r, tuple))}
    return build_response(asset, venues, failed, notional, now_ms)


def _short_reason(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return {451: "geo-blocked (451)", 403: "geo-blocked (403)", 404: "no such contract"}.get(
            code, f"http {code}")
    if isinstance(e, asyncio.TimeoutError):
        return "timeout"
    return (str(e) or type(e).__name__)[:80]
