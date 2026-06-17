# AgentPay — CMC Strategy Skill (BNB HACK Track 2)

A **CoinMarketCap Skill** that generates a backtestable, regime-gated mean-reversion **trading-strategy spec** for a BSC / PancakeSwap token — and decides what market data is even worth paying for first (**honest free-vs-paid routing**). A strategy spec, not a live-trading agent.

Built for **BNB HACK: AI Trading Agent Edition — Track 2 (Strategy Skills)**. Follows the official CoinMarketCap skill format ([coinmarketcap-official/skills-for-ai-agents-by-CoinMarketCap](https://github.com/coinmarketcap-official/skills-for-ai-agents-by-CoinMarketCap)).

## Skill

| Skill | Description |
|-------|-------------|
| [strategy-spec](skills/strategy-spec/SKILL.md) | Free regime intel + one paid CMC x402 DEX call → a regime-gated mean-reversion strategy spec, backtested over 180d against a buy-and-hold benchmark. |

## What makes it a "Strategy Skill"

It produces the Track-2 deliverable — a **backtestable strategy spec** (rules + parameters + data provenance + 180d backtest with a buy-and-hold benchmark) — and it does it with one idea most strategy generators skip: **honest routing.** Prices and market regime are free everywhere, so it never pays for them; CoinMarketCap's normalized BSC/PancakeSwap DEX liquidity has no free equivalent, so that is the one justified paid call ($0.01 via x402). The cost decision is surfaced in the spec.

## Install

Copy the skill folder into your agent's skills directory:

```bash
cp -r skills/strategy-spec /path/to/your/skills/directory/
```

The single paid step uses CoinMarketCap **x402** (pay-per-request, **no API key**):

```bash
npm install @x402/axios @x402/evm viem
```

Fund a Base wallet with a little USDC (~$0.01 per run) + ETH for gas. See [skills/strategy-spec/SKILL.md](skills/strategy-spec/SKILL.md) for the full workflow.

## Sponsor stack

- **CoinMarketCap (primary):** the one paid call is CMC's keyless x402 `dex/search` — token, price, and pool liquidity in one response — paid only because honest routing proved there's no free equivalent.
- **BNB Chain:** the strategy is BSC / PancakeSwap-native (WBNB, pool-depth-capped sizing).

## Validation

Try these prompts after installing:

- "Build a backtested strategy for BNB"
- "Give me a regime mean-reversion strategy spec for a BSC token"
- "/strategy-spec"

Expect: an honest-routing table (free vs paid), one $0.01 CMC DEX call, and a strategy spec whose backtest is reported **against buy-and-hold** (e.g. the worked example: WBNB −13.6% vs −29.3% hold — beats hold on return, Sharpe, and drawdown).

## Reference implementation

This skill is the portable form of AgentPay's flagship `strategy_spec` workflow — the honest-routing engine, the x402 CMC leg, and the backtest+benchmark are implemented and unit-tested at **https://github.com/romudille-bit/agentpay**. The agent runs it on its own x402 rails and leaves verifiable on-chain receipts at **https://agentpay.tools/ledger**.

## License

MIT
