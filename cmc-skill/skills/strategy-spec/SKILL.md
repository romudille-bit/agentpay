---
name: strategy-spec
description: |
  Generates a backtestable, regime-gated mean-reversion TRADING STRATEGY SPEC for a BSC / PancakeSwap token using CoinMarketCap x402 DEX data — and decides what data is even worth paying for first (honest free-vs-paid routing). Produces rules + parameters + a 180-day backtest with a buy-and-hold benchmark. A strategy spec, not a live-trading agent.
  Use when a user wants a backtestable crypto trading strategy, a quant-style strategy spec, a regime/mean-reversion strategy for BNB/BSC tokens, or asks "build me a strategy for X".
  Trigger: "build a strategy", "strategy spec", "backtest a strategy", "mean reversion strategy", "regime strategy for [token]", "trading strategy for BNB", "/strategy-spec"
license: MIT
compatibility: ">=1.0.0"
user-invocable: true
---

# Strategy Spec Skill — honest-routing, backtested

Generate a **backtestable trading-strategy spec** for a BSC / PancakeSwap token from CoinMarketCap data. The output is a self-contained spec (rules + parameters + data provenance + a 180-day backtest with a buy-and-hold benchmark) — quant research a human or backtester can run, **not** a live-trading agent.

This skill's distinctive principle is **honest routing**: before fetching anything, decide whether each data need is free or genuinely worth paying for. Prices and market regime are free everywhere — never pay for them. CoinMarketCap's normalized DEX pair data has no free equivalent — that is the one justified paid call. The decision is surfaced in the spec so the cost reasoning is legible.

## Prerequisites

The single paid step uses CoinMarketCap's **x402** pay-per-request DEX endpoint (**no API key required**), settled through the **AgentPay SDK** — which is what makes this an *economic-intelligence* skill, not just a payment: AgentPay enforces a hard budget cap, returns a verifiable on-chain receipt, applies the honest-routing decision, and can vet the provider with `verified_route` before paying.

```bash
pip install "agentpay-x402[base]"
```

1. A **Base wallet** with ~**$0.01 USDC** per run (one DEX call) + a little ETH for gas
2. Set `BASE_AGENT_KEY` (your funded Base private key) in the environment

Everything else (prices, Fear & Greed, funding, OHLCV history) is fetched from **free** sources — by design.

> Vendor-neutral fallback: any x402 client works against CMC's endpoint (e.g. Coinbase's `@x402/axios @x402/evm viem`), but you lose the cap, receipt, and routing layer AgentPay adds.

## Core principle — honest routing

For every data need, ask: *is this free somewhere, or do I have to pay?* Pay only when there is no free equivalent. This is what turns a pile of API calls into cost-aware quant research.

| Data need | Decision | Source | Why |
|-----------|----------|--------|-----|
| spot price | **free** | CoinGecko / any price feed | prices are free everywhere — never pay |
| market regime | **free** | Fear & Greed + perp funding | covered by free indicators |
| DEX token + pool liquidity | **paid ($0.01)** | **CMC x402 `dex/search`** | no free equivalent for normalized BSC/PancakeSwap DEX data |

One paid CMC call settles the whole DEX-data need (token, price, and liquidity come back in the same response — do **not** pay twice).

## Workflow

### Step 1 — Read the regime (FREE)

Fetch the **Fear & Greed Index** and **perp funding** for the majors. Classify the entry bias:

- Fear & Greed **≤ 25** (extreme fear) → `accumulate` (contrarian mean-reversion long bias)
- Fear & Greed **≥ 75** (extreme greed) → `trim`
- otherwise → `neutral`

Funding gate: if perp funding is crowded-long, suppress new longs (avoid buying a crowded trade).

### Step 2 — Resolve the token + liquidity (PAID — the one CMC call, via AgentPay)

Settle one CMC x402 DEX-search call **through the AgentPay SDK** — it charges the $0.01 against the hard cap and returns a receipt:

```python
from agentpay import quickstart

s = quickstart(max_spend=0.10)   # hard budget cap; uses your funded BASE_AGENT_KEY for the paid leg
# Optional: vet the marketplace before paying a stranger —
#   s.call("verified_route", {"need": "dex pair liquidity", "budget_usd": 0.05})
r = s.call("https://pro.coinmarketcap.com/x402/v1/dex/search?q=BNB")   # $0.01 USDC on Base, no CMC API key
print(r.data["data"]["tks"][0])     # token, price, liquidity
print(s.spending_summary())         # verifiable receipt: each call, cost, tx, chain
```

AgentPay signs and pays the EIP-3009 USDC authorization on Base automatically. The response (`data.tks[]`) carries the token (`n`, `s`, `addr`, `plt`), price (`pu`), and **pool liquidity (`liq`)** — everything the spec needs from one paid call. Use the top BSC/PancakeSwap match.

### Step 3 — Define the strategy rule

A regime-gated mean-reversion rule (rules only — nothing executes):

> Enter long when `fear_greed ≤ fear_entry` **AND** funding is not crowded-long. Exit when `fear_greed ≥ greed_exit` **OR** the position has been held `hold_days_max` days.

Position sizing is capped by pool liquidity so the spec is executable, not fantasy: `max_position ≤ 1% of pool liquidity` (hard cap $25k).

### Step 4 — Backtest with a buy-and-hold benchmark (FREE history)

Fetch **180 days** of daily history from free sources: token OHLCV (CoinGecko `market_chart`) + daily Fear & Greed (alternative.me `fng`). Then:

1. **Sweep the parameters** — `fear_entry ∈ {15,20,25}`, `greed_exit ∈ {70,75,80}`, `hold_days_max ∈ {7,14,30}` (27 combinations).
2. For each set, simulate the rule and compute **total return, Sharpe (annualized, rf=0), max drawdown, win rate, exposure**.
3. **Rank by Sharpe** (risk-adjusted, not the luckiest high-variance run).
4. **ALWAYS compare against buy-and-hold** over the same window. Report the strategy *and* the benchmark *and* the edge (excess return, excess Sharpe, drawdown reduction). Never report a strategy's return without the hold benchmark — that is the honest yardstick.

### Step 5 — Assemble the spec

Emit the backtestable strategy spec (see template below). It includes the rules, the parameter grid, the **data provenance** (the honest-routing table — what was free vs paid and why), the **cost receipt**, and the **backtest results + benchmark + edge**.

## Output — strategy spec template

```json
{
  "kind": "strategy_spec",
  "name": "regime-gated mean-reversion on WBNB (BSC/PancakeSwap)",
  "venue": { "chain": "BSC", "dex": "PancakeSwap" },
  "universe": [{ "symbol": "WBNB", "address": "0x…", "network": "BSC" }],
  "thesis": "Buy a quality liquid BSC token into extreme fear, scale out into greed; gate by perp funding to avoid crowded longs.",
  "signal": { "fear_greed": 22, "entry_bias": "accumulate",
              "rule": "enter when fear_greed<=25 AND funding not crowded-long; exit when fear_greed>=75 OR hold_days_max" },
  "execution": { "venue": "PancakeSwap on BSC", "liquidity_usd": 54804941,
                 "max_position_usd": 25000, "sizing_rule": "<=1% of pool liquidity, hard cap $25k" },
  "parameters_to_sweep": { "fear_entry": [15,20,25], "greed_exit": [70,75,80], "hold_days_max": [7,14,30] },
  "data_provenance": [
    { "need": "spot_price",    "decision": "free", "source": "coingecko" },
    { "need": "market_regime", "decision": "free", "source": "fear_greed+funding" },
    { "need": "dex_liquidity", "decision": "paid", "source": "cmc:x402:dex/search", "why": "no free equivalent" }
  ],
  "cost": { "spent_usdc": "0.01", "note": "free for prices/regime; one paid CMC DEX call" },
  "backtest": {
    "window": "180d daily",
    "results": {
      "best_params": { "fear_entry": 25, "greed_exit": 70, "hold_days_max": 14 },
      "total_return_pct": -13.58, "sharpe": -0.43, "max_drawdown_pct": 30.44,
      "win_rate_pct": 60.0, "n_trades": 10, "combos_tested": 27,
      "benchmark": { "kind": "buy_and_hold", "total_return_pct": -29.30, "sharpe": -1.19, "max_drawdown_pct": 39.75 },
      "edge": { "excess_return_pct": 15.72, "excess_sharpe": 0.76, "drawdown_reduction_pct": 9.31,
                "beats_hold_return": true, "beats_hold_sharpe": true, "lower_drawdown": true }
    },
    "executes_live": false
  }
}
```

## Worked example (real run, 2026-06-17)

Target **BNB/BSC**, Fear & Greed **22 (Extreme Fear)** → `accumulate`. One $0.01 CMC `dex/search` returned **WBNB**, pool liquidity **$54.8M**. Backtest over 180d, best of 27 param sets:

| Metric | Strategy | Buy & hold | Edge |
|--------|----------|------------|------|
| Return | −13.6% | −29.3% | **+15.7%** |
| Sharpe | −0.43 | −1.19 | **+0.76** |
| Max drawdown | 30.4% | 39.8% | **9.3% smaller** |

BNB fell ~29% over the window; the regime gate **halved the loss with a smaller drawdown and a +0.76 Sharpe edge.** Total data cost: **$0.01**.

## Important notes

- **This is research, not financial advice**, and a **spec, not a live agent** — nothing executes on-chain.
- **Always report the buy-and-hold benchmark.** A strategy that loses money can still be the right call if it loses less than holding, with a smaller drawdown — and showing the real benchmark (not a cherry-picked bull window) is the credibility signal.
- **Honest routing is the discipline.** If a data need is free, route it free — including away from CMC and away from any paid tool. Only the DEX liquidity leg is worth paying CMC for.

## Handling failures

- **CMC x402 `dex/search` fails / no match**: fall back to a free DEX price source for liquidity; mark `dex_liquidity` as `degraded` in `data_provenance` and proceed — the regime gate + backtest still produce a valid spec.
- **History fetch fails (<30 bars)**: ship the spec with `parameters_to_sweep` but no `results`, and note "backtest skipped — insufficient history."
- **Fear & Greed unavailable**: set `entry_bias = neutral` and note the missing regime input.

## Reference

This skill is the reusable, portable form of AgentPay's flagship `strategy_spec` workflow — the honest-routing engine, the x402 CMC leg, and the backtest+benchmark are implemented and unit-tested at https://github.com/romudille-bit/agentpay (the agent runs it daily and leaves on-chain receipts at https://agentpay.tools/ledger). CMC x402 docs: https://coinmarketcap.com/api/documentation/ai-agent-hub/x402
