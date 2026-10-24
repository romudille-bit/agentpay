"""
services/bitflow.py — Bitflow (Stacks DEX) venue for pre_trade_check.

Keyless public endpoints only: the HODLMM quote API for a route quote at the
caller's size, the pool detail for TVL / 24h volume, and the core token list
for symbol → contract. Price impact is computed here (quote at size vs a
small reference quote), not read from Bitflow. Thresholds and the reasoning
are in docs/bitflow-venue.md.
"""

import asyncio
import logging

import httpx

from gateway.services.cache import cache_get, cache_set

logger = logging.getLogger(__name__)

HODLMM_API = "https://bff.bitflowapis.finance"
CORE_API = "https://api.bitflowapis.finance"

STX = "SM1793C4R5PZ4NS4VQ4WMP7SKKYVH8JZEWSZ9HCCR.token-stx-v-1-2"
SBTC = "SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token"
USDCX = "SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx"

# Seed for the HODLMM token set (what the quote API can route), used when
# the live list is unreachable. symbol → (contract, decimals).
_SEED_TOKENS: dict[str, tuple[str, int]] = {
    "STX":    (STX, 6),
    "SBTC":   (SBTC, 8),
    "USDCX":  (USDCX, 6),
    "STSTX":  ("SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.ststx-token", 6),
    "STBTC":  ("SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.stbtc-token", 8),
    "USDH":   ("SPN5AKG35QZSK2M8GAMR4AFX45659RJHDW353HSG.usdh-token-v1", 8),
    "AEUSDC": ("SP3Y2ZSH8P7D50B0VBTSX11S7XSG24M1VB9YFQA4K.token-aeusdc", 6),
    "ZEST":   ("SP1A27KFY4XERQCCRCARCYD1CC5N7M6688BSYADJ7.zest-token", 6),
    "LEO":    ("SP1AY6K3PQV5MRT6R4S671NWW2FRVPKM0BR162CT6.leo-token", 6),
}
_ALIASES = {"USDC": "USDCX"}

REF_USD = 10.0           # reference quote size for the mid price
IMPACT_CAUTION_PCT = 1.0  # bands follow the AIBTC Bitflow skill (low <1%, medium <3%)
IMPACT_AVOID_PCT = 3.0
TVL_CAUTION_MULT = 10     # pool TVL under 10× the order → caution


async def _get_json(client: httpx.AsyncClient, url: str, **kw) -> dict | list | None:
    try:
        r = await client.get(url, headers={"Accept": "application/json"}, **kw)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[bitflow] GET {url} failed: {e}")
        return None


async def hodlmm_tokens(client: httpx.AsyncClient) -> dict[str, tuple[str, int]]:
    """symbol → (contract, decimals) for tokens the quote API can route."""
    cached = cache_get("bitflow:hodlmm_tokens")
    if cached:
        return {k: tuple(v) for k, v in cached["tokens"].items()}
    data = await _get_json(client, f"{HODLMM_API}/api/quotes/v1/tokens?amm_type=dlmm")
    if not isinstance(data, dict) or not data.get("tokens"):
        return dict(_SEED_TOKENS)
    out = dict(_SEED_TOKENS)
    for t in data["tokens"]:
        sym, contract = t.get("symbol"), t.get("contract_address")
        if sym and contract:
            out[sym.upper()] = (contract, int(t.get("decimals") or 6))
    cache_set("bitflow:hodlmm_tokens", {"tokens": {k: list(v) for k, v in out.items()}}, 3600)
    return out


async def core_symbols(client: httpx.AsyncClient) -> dict[str, str]:
    """symbol → contract for every token Bitflow lists (xyk + stableswap + HODLMM).

    Used only to recognise a Stacks token we cannot quote yet.
    """
    cached = cache_get("bitflow:core_symbols")
    if cached:
        return cached["symbols"]
    data = await _get_json(client, f"{CORE_API}/getAllTokensAndPools")
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for t in data.get("tokens") or []:
            sym = (t.get("symbol") or "").upper()
            if sym and t.get("status", "active") == "active":
                out[sym] = t.get("tokenContract") or t.get("token-id") or ""
    if out:
        cache_set("bitflow:core_symbols", {"symbols": out}, 3600)
    return out


async def resolve(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """Classify a symbol or contract id as a Stacks token.

    Returns {"symbol", "contract", "decimals", "quotable"} or None when
    Bitflow does not list it at all.
    """
    key = _ALIASES.get(symbol.upper(), symbol.upper())
    tokens = await hodlmm_tokens(client)
    if key in tokens:
        contract, dec = tokens[key]
        return {"symbol": key, "contract": contract, "decimals": dec, "quotable": True}
    if "." in symbol:
        for sym, (contract, dec) in tokens.items():
            if contract.lower() == symbol.lower():
                return {"symbol": sym, "contract": contract, "decimals": dec, "quotable": True}
    listed = await core_symbols(client)
    if key in listed:
        return {"symbol": key, "contract": listed[key], "decimals": None, "quotable": False}
    return None


async def quote(client: httpx.AsyncClient, token_in: str, token_out: str,
                amount_in: int) -> dict:
    """Best route for amount_in (atomic units) → {amount_out, pools, hops, ...}."""
    body = {"input_token": token_in, "output_token": token_out,
            "amount_in": str(int(amount_in)), "amm_strategy": "best",
            "slippage_tolerance": 1}
    try:
        r = await client.post(f"{HODLMM_API}/api/quotes/v1/quote/multi", json=body)
        data = r.json() if r.content else {}
    except Exception as e:
        return {"error": f"quote request failed: {str(e)[:80]}"}
    if r.status_code != 200 or not data.get("success"):
        return {"error": f"no route: {data.get('error') or data.get('detail') or r.status_code}"}
    routes = [x for x in data.get("routes") or [] if int(x.get("amount_out") or 0) > 0]
    if not routes:
        return {"error": "no route with output at this size"}
    best = max(routes, key=lambda x: int(x["amount_out"]))
    ed = best.get("execution_details") or {}
    hops = ed.get("hop_details") or [ed]
    pools = []
    for h in hops:
        pid = h.get("pool_id")
        if pid and pid not in pools:
            pools.append(pid)
    return {
        "amount_out":      int(best["amount_out"]),
        "route_path":      best.get("route_path") or [],
        "hops":            int(best.get("total_hops") or len(hops) or 1),
        "pools":           pools,
        "bins_traversed":  sum(int(h.get("bins_traversed") or 0) for h in hops),
        "fee_rate":        ed.get("fee_rate"),
        "bitflow_impact_bps": int(best.get("price_impact_bps") or 0),
    }


async def pool_stats(client: httpx.AsyncClient, pool_id: str) -> dict:
    cached = cache_get(f"bitflow:pool:{pool_id}")
    if cached:
        return cached
    data = await _get_json(client, f"{HODLMM_API}/api/app/v1/pools/{pool_id}")
    if not isinstance(data, dict) or data.get("tvlUsd") is None:
        return {"pool_id": pool_id, "tvl_usd": None, "volume_usd_1d": None}
    out = {"pool_id": pool_id,
           "pool_contract": data.get("poolContract"),
           "tvl_usd": round(float(data["tvlUsd"]), 2),
           "volume_usd_1d": round(float(data.get("volumeUsd1d") or 0), 2),
           "bin_step": data.get("binStep")}
    cache_set(f"bitflow:pool:{pool_id}", out, 300)
    return out


def _band(impact_pct: float) -> str:
    if impact_pct >= IMPACT_AVOID_PCT:
        return "avoid"
    if impact_pct >= IMPACT_CAUTION_PCT:
        return "caution"
    return "ok"


async def liquidity_factor(client: httpx.AsyncClient, token: dict, side: str,
                           size_usd: float) -> dict:
    """The pre_trade_check liquidity factor for a Bitflow swap at size.

    Long = buy `token` with the quote asset; short = sell it for the quote
    asset. Impact = 1 − (rate at size / rate at a $10 reference), so the
    pool fee cancels out and only the size effect remains.
    """
    base = {"venue": "bitflow", "symbol": token["symbol"]}
    if not token.get("quotable"):
        return {**base, "level": "unknown",
                "reason": f"{token['symbol']} trades on Bitflow xyk pools only — "
                          "not quotable through the HODLMM quote API; impact unmeasured"}
    contract, dec = token["contract"], token["decimals"]
    # USDCx (≈ $1) is the quote asset; the USDCx check itself quotes against STX.
    quote_asset, qdec = (STX, 6) if contract == USDCX else (USDCX, 6)
    base["quote_asset"] = "STX" if quote_asset == STX else "USDCx"
    quote_unit_usd = 1.0
    if quote_asset == STX:
        back = await quote(client, USDCX, STX, int(REF_USD * 10 ** 6))
        if back.get("error"):
            return {**base, "level": "unknown", "reason": f"Bitflow quote unavailable: {back['error']}"}
        quote_unit_usd = REF_USD / (back["amount_out"] / 10 ** 6)

    ref_in = int(REF_USD / quote_unit_usd * 10 ** qdec)
    ref = await quote(client, quote_asset, contract, ref_in)
    if ref.get("error"):
        return {**base, "level": "unknown", "reason": f"Bitflow quote unavailable: {ref['error']}"}
    mid_price = REF_USD / (ref["amount_out"] / 10 ** dec)
    verb = "sell" if side == "short" else "buy"

    if side == "short":
        amount_in = int(size_usd / mid_price * 10 ** dec)
        ref_in2 = int(REF_USD / mid_price * 10 ** dec)
        sized, ref2 = await asyncio.gather(quote(client, contract, quote_asset, amount_in),
                                           quote(client, contract, quote_asset, ref_in2))
        err = sized.get("error") or ref2.get("error")
        ref_rate = None if err else ref2["amount_out"] / ref_in2
    else:
        amount_in = int(size_usd / quote_unit_usd * 10 ** qdec)
        sized = await quote(client, quote_asset, contract, amount_in)
        err = sized.get("error")
        ref_rate = ref["amount_out"] / ref_in
    if err:
        return {**base, "level": "avoid", "mid_price_usd": round(mid_price, 8),
                "reason": f"no Bitflow route to {verb} ~${size_usd:,.0f} of {token['symbol']}: {err}"}
    size_rate = sized["amount_out"] / amount_in
    impact_pct = round(max(0.0, (1 - size_rate / ref_rate) * 100), 4) if ref_rate else None
    pools = [await pool_stats(client, p) for p in sized["pools"]]
    tvls = [p["tvl_usd"] for p in pools if p.get("tvl_usd") is not None]
    min_tvl = min(tvls) if tvls else None

    out = {
        **base,
        "price_impact_pct":   impact_pct,
        "mid_price_usd":      round(mid_price, 8),
        "effective_price_usd": round(mid_price * (1 + (impact_pct or 0) / 100), 8),
        "route":              sized["route_path"],
        "hops":               sized["hops"],
        "pools":              pools,
        "bins_traversed":     sized["bins_traversed"],
        "fee_rate":           sized["fee_rate"],
        "bitflow_reported_impact_bps": sized["bitflow_impact_bps"],
    }
    if impact_pct is None:
        out.update(level="unknown", reason="could not derive price impact from the quotes")
        return out
    level = _band(impact_pct)
    reason = f"~{impact_pct}% price impact on ~${size_usd:,.0f} via {sized['hops']} hop(s)"
    if min_tvl is not None and min_tvl < size_usd * TVL_CAUTION_MULT and level == "ok":
        level = "caution"
        reason += f"; thinnest pool on the route holds ${min_tvl:,.0f} TVL (< {TVL_CAUTION_MULT}× the order)"
    elif min_tvl is not None:
        reason += f"; thinnest pool TVL ${min_tvl:,.0f}"
    out.update(level=level, reason=reason)
    return out
