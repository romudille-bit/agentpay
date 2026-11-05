# funding_spread

Cross-venue perp funding divergence for one asset, priced at $0.01. The tool
answers "which venue pays the most and the least for this perp right now, is
that wide, and what is the carry worth at my size" — not the raw rates
(`funding_rates` is free for that).

## Venues

Binance, Bybit, OKX, Gate.io, Bitget, Hyperliquid, dYdX, MEXC, KuCoin — all
public, keyless. Each is read in parallel with a 12 s budget; a venue that
fails is named in `venues_unavailable` with a short reason and the spread is
computed over the rest. Fewer than two answering venues is an error, not a
spread. Binance and Bybit refuse US egress (451 / 403); they join
automatically when the gateway runs from another region.

USDT-margined linear perps everywhere (`BTC-USDT-SWAP` on OKX, `BTC_USDT` on
Gate and MEXC, `XBTUSDTM` on KuCoin, `BTC-USD` on dYdX).

## Normalisation

Venues settle on different intervals: 8h on most, 1h on Hyperliquid and
dYdX, 4h on some Bitget/Gate contracts. Every rate is scaled to a per-8h
figure (`rate_8h_pct = raw_rate_pct × 8 / interval_hours`) before any
comparison; `raw_rate_pct` and `interval_hours` are kept so the caller can
undo it. `rate_annualized_pct = rate_8h_pct × 3 × 365`.

## Spread

`spread.pct_8h` is the dearest venue minus the cheapest; `bps_8h` is the same
in basis points. `median_8h_pct` and `max_deviation_from_median_pct` say
whether one venue is the outlier or the whole set is dispersed.

## History percentile

Each venue's last 7 days of settlements are averaged into 8h buckets; the
cross-venue spread is computed per bucket over the venues that have history
in it (buckets with a single venue are skipped). `current_percentile` is the
share of those historic spreads at or below the current one, so "90th
percentile" means wider than 9 of 10 recent readings. Venues without a
history endpoint, or whose history call failed, count for the current spread
but not for the percentile (`venues_with_history` lists who did).

## Carry estimate

With `notional_usd`: long the cheapest venue, short the dearest, each at that
notional. `per_8h_usd = spread × notional`, `per_day_usd` is three periods,
`per_30d_usd` ninety. `taker_fees_round_trip_usd` assumes 0.05 % taker on
every leg — two venues, open and close, four legs — and `breakeven_hours` is
how long the spread must hold to cover them. The spread is assumed constant
and no slippage or basis move is modelled; this is a sizing aid, not a P&L.

## Caching

Responses are cached for 2 minutes per asset+notional (`CACHE_TTL`).
