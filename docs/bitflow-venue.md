# pre_trade_check on Bitflow — the Stacks DEX venue

`pre_trade_check` reads centralised order books (Binance / Bybit / OKX). For
the tokens Stacks agents actually trade — sBTC, STX, USDCx, stSTX, ZEST, LEO,
USDh, aeUSDC, stBTC — the book that matters is a Bitflow pool. This venue
prices the swap there. Code: `gateway/services/bitflow.py`; wiring in
`_fetch_pre_trade_check` (`gateway/services/tools_runtime.py`).

## Venue selection

| Symbol | Venue (`auto`) | liquidity factor | carry / crowding | security |
|---|---|---|---|---|
| ETH, SOL, … (CEX majors) | `cex` | CEX book, unchanged | CEX perps | as before |
| STX, sBTC | `cex+bitflow` | CEX book in `liquidity`, Bitflow in `dex_liquidity` | CEX perps (sBTC reads BTC's) | `n/a` — native Stacks assets |
| ZEST, LEO, USDCx, … (DEX-only) | `bitflow` | Bitflow in `liquidity` | `n/a` — no perpetual market | `unknown` — GoPlus has no Stacks coverage, so the contract is **not** screened and the verdict is capped at `caution` |
| WELSH and other xyk-only listings | `bitflow` | `unknown` — not quotable yet | `n/a` | `unknown` |

`venue: "bitflow"` prices the swap on Bitflow only (perp context kept when a
perp exists); `venue: "cex"` skips Bitflow. The response carries `venue` and
`not_checked` (every factor that is `n/a` or `unknown`), so a reader can see
which factors were measured. `n/a` is excluded from the verdict; `unknown`
counts — missing data is never `ok`.

## Data (all keyless, public)

- Route quote at size: `POST https://bff.bitflowapis.finance/api/quotes/v1/quote/multi`
  (`input_token`, `output_token`, `amount_in` in atomic units, `amm_strategy: best`).
  Covers the HODLMM (DLMM) pools, which is where the liquidity for the tokens
  above lives. Returns every candidate route with `amount_out`, the pools and
  bins crossed, and Bitflow's own `price_impact_bps`.
- Pool TVL / 24h volume: `GET https://bff.bitflowapis.finance/api/app/v1/pools/{pool_id}`.
- Token lists: `GET …/api/quotes/v1/tokens?amm_type=dlmm` (quotable set) and
  `GET https://api.bitflowapis.finance/getAllTokensAndPools` (everything Bitflow
  lists; used only to recognise a Stacks token we cannot quote). Both cached 1h.
- The SDK's own quote path (`getQuoteForRoute`) is read-only Clarity calls per
  hop; the public `/ticker` only lists the older xyk / stableswap pools. Neither
  is used.

## Impact

USDCx is the quote asset (STX for the USDCx check itself). A $10 reference
quote gives the mid price; the sized quote in the same direction gives the
effective price. `price_impact_pct = 1 − rate(size) / rate($10)`, so the pool
fee (0.5 % on the sBTC/STX pool) cancels and only the size effect remains.
`bitflow_reported_impact_bps` is kept alongside for comparison; on 24 Oct 2026
a $30k STX buy read 0.62 % here against 42 bps from Bitflow.

Bands follow the severity the AIBTC Bitflow skill shows Stacks agents, so the
verdict matches what they already see:

| impact | level |
|---|---|
| < 1 % | `ok` |
| 1 – 3 % | `caution` |
| ≥ 3 % | `avoid` |

An `ok` is downgraded to `caution` when the thinnest pool on the route holds
less than 10× the order in TVL. No route at size → `avoid`; quote API down →
`unknown`.

Reference points (24 Oct 2026, mainnet): STX $1k → 0.04 %; STX $30k → 0.62 %;
sBTC short $5k → 0.43 % (2 hops via STX); ZEST $300 → 0.71 % (3 hops);
USDCx $2k → 0.09 %.

## Not done yet

- xyk / stableswap-only tokens (WELSH, memecoins) read `unknown`: quoting them
  needs the per-hop read-only `get-quote-a` call through a Stacks node.
- No Bitflow API key; the public limit is 500 requests/min per IP, each check
  spends 3–4.
