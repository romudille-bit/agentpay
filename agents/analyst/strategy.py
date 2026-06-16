#!/usr/bin/env python3
"""
strategy.py — flagship v2 "strategy spec" helpers (pure, unit-tested).

The hackathon goal (BNB×CMC×Trust Wallet, Track 2 "Strategy Skills") asks for a
*backtestable strategy spec, not a live-trading agent*. This module holds the
pure logic the agent uses to produce one honestly:

  - HONEST ROUTING: for each data need, decide free-vs-paid by the cost-of-
    self-serve test. Prices/quotes are free everywhere (AgentPay token_price,
    CoinGecko) → never pay. CMC's DEX pair liquidity has no free AgentPay
    equivalent → a justified paid call. The routing table is surfaced in the
    output so the cost decision is legible ("price: free; DEX pairs: paid CMC").

  - CMC x402 endpoints: URL builders for the keyless pay-per-request endpoints
    (Base / USDC / EIP-3009, $0.01) — settle-compatible with the AgentPay SDK.

  - STRATEGY SPEC: assemble a backtestable spec (rules + parameters + data
    provenance + cost receipt). No live execution — just a spec a human or
    backtester can run.

All functions here are pure (no I/O) so they're testable without a wallet or
network. The agent (run.py) does the I/O and hands data in.
"""

from __future__ import annotations

from typing import Optional

# CMC x402 — keyless pay-per-request, Base/USDC/EIP-3009, $0.01/call.
# Probed 2026-06-15: settle-compatible with the AgentPay SDK's Mode A.
CMC_X402_BASE = "https://pro-api.coinmarketcap.com"
CMC_X402 = {
    "quotes":     "/x402/v3/cryptocurrency/quotes/latest",
    "listings":   "/x402/v3/cryptocurrency/listings/latest",
    "dex_search": "/x402/v1/dex/search",
    "dex_pairs":  "/x402/v4/dex/pairs/quotes/latest",
}
CMC_X402_PAYTO = "0x271189c860DB25bC43173B0335784aD68a680908"


def cmc_url(endpoint: str, params: Optional[dict] = None) -> str:
    """Build a full CMC x402 URL. `endpoint` is a key of CMC_X402 or a raw path."""
    import urllib.parse
    path = CMC_X402.get(endpoint, endpoint)
    qs = f"?{urllib.parse.urlencode(params)}" if params else ""
    return f"{CMC_X402_BASE}{path}{qs}"


# ── Honest routing ─────────────────────────────────────────────────────────────
# Decide on USAGE-of-money, not brand. Pay only when there is no free equivalent
# AND the agent can't trivially self-serve. (See SPEC_TRUST_ORACLE.html.)

# Each rule: a data need → decision + the cheapest source that satisfies it + why.
ROUTING_RULES: list[dict] = [
    {
        "need": "spot_price",
        "decision": "free",
        "source": "agentpay:token_price",
        "why": "prices are free everywhere (AgentPay token_price / CoinGecko) — never pay",
    },
    {
        "need": "market_regime",
        "decision": "free",
        "source": "agentpay:fear_greed_index+funding_rates",
        "why": "regime read is covered by the free intel tier",
    },
    {
        "need": "dex_pair_liquidity",
        "decision": "paid",
        "source": "cmc:dex_pairs",
        "why": "no free AgentPay equivalent (orderbook_depth is CEX-only); CMC's "
               "normalized DEX pair data — incl. PancakeSwap pools on BSC — is "
               "the vetted source",
    },
    {
        "need": "dex_token_discovery",
        "decision": "paid",
        "source": "cmc:dex_search",
        "why": "AgentPay has no DEX token search; CMC's keyless x402 search "
               "(BSC/PancakeSwap-aware) is cheaper than stitching free DEX "
               "scrapers + reconciling",
    },
]

_RULES_BY_NEED = {r["need"]: r for r in ROUTING_RULES}


def route_decision(need: str) -> dict:
    """The free-vs-paid decision for one data need. Unknown needs default to free
    (never pay for something we can't justify)."""
    return _RULES_BY_NEED.get(need, {
        "need": need, "decision": "free", "source": "agentpay/self",
        "why": "no justified paid source — default to free / self-serve",
    })


def routing_table(needs: list[str]) -> list[dict]:
    """The honest routing decisions for a plan's data needs — surfaced in output
    so the cost reasoning is legible on screen."""
    return [route_decision(n) for n in needs]


def paid_needs(needs: list[str]) -> list[str]:
    """Just the needs whose honest decision is to pay."""
    return [n for n in needs if route_decision(n)["decision"] == "paid"]


# ── CMC response parsing (defensive — CMC envelopes vary) ──────────────────────

def parse_dex_search(data: dict | None) -> list[dict]:
    """Pull a compact list of DEX matches from a CMC dex/search response.

    CMC wraps results in a `data` envelope; shapes vary, so be tolerant and
    extract only the fields we use (name, symbol, address, network)."""
    if not isinstance(data, dict):
        return []
    rows = data.get("data") or data.get("results") or []
    if isinstance(rows, dict):                      # some endpoints key by id
        rows = list(rows.values())
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        out.append({
            "name":    r.get("name") or r.get("base_asset_name"),
            "symbol":  r.get("symbol") or r.get("base_asset_symbol"),
            "address": r.get("contract_address") or r.get("address")
                       or r.get("base_asset_contract_address"),
            "network": r.get("network_slug") or r.get("network") or r.get("platform"),
        })
    return out


def parse_dex_pair(data: dict | None) -> dict:
    """Pull price/liquidity/volume from a CMC dex/pairs quote (defensive)."""
    if not isinstance(data, dict):
        return {}
    rows = data.get("data") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    row = (rows[0] if isinstance(rows, list) and rows else
           rows if isinstance(rows, dict) else {})
    if not isinstance(row, dict):
        return {}
    quote = row.get("quote")
    q0 = (quote[0] if isinstance(quote, list) and quote else
          quote if isinstance(quote, dict) else {}) or {}
    return {
        "price_usd":     q0.get("price") or row.get("price"),
        "liquidity_usd": q0.get("liquidity") or row.get("liquidity"),
        "volume_24h":    q0.get("volume_24h") or row.get("volume_24h"),
        "pair_address":  row.get("contract_address") or row.get("pair_address"),
    }


# ── Strategy spec assembly ─────────────────────────────────────────────────────

def regime_gate(fear_greed_value: Optional[int], funding_bias: Optional[str]) -> dict:
    """Turn the free regime read into a backtestable entry gate. Pure, rules-only
    — this is a spec, not a trade."""
    fg = fear_greed_value if isinstance(fear_greed_value, (int, float)) else None
    # Classic contrarian-on-extremes gate; thresholds are explicit so a backtest
    # can sweep them. Nothing executes here.
    if fg is None:
        bias = "neutral"
    elif fg <= 25:
        bias = "accumulate"        # extreme fear → mean-reversion long bias
    elif fg >= 75:
        bias = "trim"              # extreme greed → reduce
    else:
        bias = "neutral"
    return {
        "fear_greed": fg,
        "funding_bias": funding_bias,
        "entry_bias": bias,
        "rule": "enter only when fear_greed<=25 AND funding not crowded-long; "
                "exit when fear_greed>=75 OR funding flips crowded-long",
    }


# Default execution venue — the BNB sponsor stack (BSC / PancakeSwap).
VENUE = {"chain": "BSC", "dex": "PancakeSwap"}


def build_strategy_spec(
    *,
    token: dict,
    regime: dict,
    liquidity: dict,
    routing: list[dict],
    receipt: dict,
    run_at: str,
    backtest_results: Optional[dict] = None,
    venue: Optional[dict] = None,
) -> dict:
    """Assemble the backtestable strategy spec. PURE.

    `token` = {name,symbol,address,network} (from CMC dex_search),
    `regime` = regime_gate(...) output,
    `liquidity` = parse_dex_pair(...) output,
    `routing` = routing_table(...) (the honest free-vs-paid decisions),
    `receipt` = session.spending_summary(),
    `backtest_results` = backtest.summarize(sweep(...)) — the executed sweep's
        best params + metrics. When omitted the spec still ships, but with no
        results (the recipe without the cooked dish); supply it for a quant-
        grade submission.
    `venue` = execution venue (defaults to BSC / PancakeSwap, the BNB stack).
    """
    venue = venue or VENUE
    liq = liquidity.get("liquidity_usd")
    # Position sizing capped by liquidity so the spec is executable, not fantasy.
    max_size = None
    if isinstance(liq, (int, float)) and liq > 0:
        max_size = round(min(liq * 0.01, 25_000), 2)   # ≤1% of pool, ≤$25k
    backtest_block = {
        "window": "180d daily",
        "data_needed": ["daily fear_greed", "token daily OHLCV", "pool liquidity"],
        "executes_live": False,
    }
    if backtest_results:
        # The sweep was actually run — surface the results so the spec is
        # backtested, not merely backtestable.
        backtest_block["results"] = backtest_results
    return {
        "kind": "strategy_spec",
        "name": f"regime-gated mean-reversion on {token.get('symbol') or 'TOKEN'} "
                f"({venue['chain']}/{venue['dex']})",
        "run_at": run_at,
        "venue": venue,
        "universe": [token],
        "thesis": f"Buy a quality liquid {venue['chain']} token ({venue['dex']} "
                  "pool depth via CMC) into extreme fear, scale out into greed; "
                  "gate by perp funding to avoid crowded longs.",
        "signal": regime,
        "execution": {
            "venue": f"{venue['dex']} on {venue['chain']}",
            "max_position_usd": max_size,
            "sizing_rule": "<=1% of pool liquidity, hard cap $25k",
            "liquidity_usd": liq,
            "slippage_guard": "skip if 1% notional moves price > 0.5%",
        },
        "parameters_to_sweep": {
            "fear_entry": [15, 20, 25],
            "greed_exit": [70, 75, 80],
            "hold_days_max": [7, 14, 30],
        },
        "data_provenance": routing,
        "cost": {
            "spent_usdc": receipt.get("spent"),
            "budget_usdc": receipt.get("budget"),
            "calls": receipt.get("calls"),
            "note": "free tools for prices/regime; paid only for CMC DEX data "
                    "(no free equivalent)",
        },
        "backtest": backtest_block,
    }
