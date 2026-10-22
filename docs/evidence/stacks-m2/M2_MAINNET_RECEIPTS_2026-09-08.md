# M2 evidence — Stacks mainnet receipts

Three sBTC payments settled from a budget-capped `agentpay` Session against
the production gateway (`https://agentpay.tools`), each followed by the same
tool refused client-side under a per-tool cap below its price, with nothing
signed. Payer `SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5` (Leather account),
payee `SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C`. All three are on
https://agentpay.tools/ledger and `/ledger.json` as `network: stacks` legs.

Verified on Hiro (2026-09-08):

| # | txid | block | fee (µSTX) | settle path |
|---|------|-------|-----------:|-------------|
| 1 | `30689b5ee9f779fe571f0b7797e2453b21cf7d220cadf4f1881586757347387f` | 8937021 | 100,000 | uncertain → redeemed from the txid alone (AGE-147) |
| 2 | `d1de1a792e2a23f5a0b22651eec4f3f00a49f3e9ca15c70c2a19fc8fb474dd8e` | 8938646 | 3,910 | inline |
| 3 | `59ce70146da57ffd8c71cd67eea9169373ad11b7298e77a88e5f946a0064e325` | 8944701 | 50,850 | inline, on the settle-deadline build (AGE-150) |

Each is `SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token::transfer`
(amount u13, sender SP27VC…, recipient SP23XK…, memo = the challenge id),
result `(ok true)`. Explorer:
`https://explorer.hiro.so/txid/0x<txid>?chain=mainnet`.

The over-cap refusal (Endowment's four-point interpretation, confirmed
2026-07-14) is in block 2 of every run: (a) the attempted call
`pre_trade_check`, (b) the configured per-tool cap $0.005, (c) the
rejection `BudgetExceeded`, (d) `calls: 0 / spent: $0` — no settlement.

Gateway commits: receipt 1 on `71476e2` (batch 2), receipt 2 on `b434479`,
receipt 3 on `4063223` (batch 3). Command for every run:

```
export STACKS_AGENT_KEY=$(python tools/stacks_derive_key.py --address SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5 --print-key)
STACKS_NETWORK=mainnet python examples/stacks_m1_demo.py
```

---

## Receipt 1 — 2026-09-07 12:48 UTC (first mainnet settle)

The gateway's confirmation poll could not see the confirmation (Hiro 302 on
the bare-hex path, fixed in `33fdb8b`) and the redeem reply was a 502 that
Cloudflare replaced with its own page (fixed in the same commit). The payment
was correct on-chain throughout; the row sat in `payment_logs` as
`uncertain` until redeemed.

```
====================================================================
1) BUDGET-CAPPED SESSION  ->  sBTC PAYMENT ON STACKS MAINNET
====================================================================
payer (Stacks mainnet): SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5   gateway: https://agentpay.tools
session cap: $0.05   paying pre_trade_check({'symbol': 'BTC'}) in sBTC ...

  ✓ sBTC PAYMENT BROADCAST — confirming on-chain
  TX     : 30689b5ee9f779fe571f0b7797e2453b21cf7d220cadf4f1881586757347387f
  verify : https://explorer.hiro.so/txid/0x30689b5ee9f779fe571f0b7797e2453b21cf7d220cadf4f1881586757347387f?chain=mainnet
  \ waiting for confirmation, then redeeming the same signed payment
  ✗ redeem refused: redeem refused: <!DOCTYPE html>
  ...
```

Redeemed 2026-09-07 18:06 UTC after the fix was deployed, from the txid
alone (signed bytes read back from Hiro, memo = challenge id):

```
====================================================================
REDEEM FROM CHAIN  ->  30689b5ee9f779fe… on STACKS MAINNET
====================================================================
  ✓ REDEEMED — tool result delivered for the confirmed payment
  RESULT : {'symbol': 'BTC', 'side': 'long', 'size_usd': 10000.0, 'verdict': 'ok', 'factors': {'liquidity': {'bucket_usd': 10000, 'slippage_pct': 0.0, 'spread_pct': 0.0, 'exchange': 'Binance', 'level': 'ok', 'reason': 'fills within 0.0% of best price'}, 'carry': {'median_funding_pct': 0.002649, 'annualized_pct': 2.9, 'venues': 3, 'level': 'ok', 'reason': 'carry unremarkable'}, 'crowding': {'long_short_ratio': 1.177, 'oi_change_24h_pct': 1.36, 'total_oi_usd': 8537086483.0, 'level': 'ok', 'reason': 'positioning unremarkable'}, 'security': {'level': 'n/a', 'reason': 'BTC is a native chain asset — no token contract to screen'}}, 'components': {'orderbook_depth': {'asset': 'BTC', 'pair': 'BTC/USDT', 'exchange': 'Binance', 'best_ask': 79160.32, 'best_bid': 79160.31, 'spread_pct': 0.0, 'depth': [{'notional_usd': 10000, 'slippage_pct': 0.0, 'executable': True}, {'notional_usd': 50000, 'slippage_pct': 0.0, 'executable': True}, {'notional_usd': 250000, 'slippage_pct': 0.0, 'executable': True}], 'exchanges': [{'exchange': 'Binance', 'best_ask': 79160.32, 'best_bid': 79160.31}, {'exchange': 'Bybit', 'best_ask': 79157.6, 'best_bid': 79157.5}], 'source': 'binance/bybit'}, 'funding_rates': {'asset': 'BTC', 'rates': [{'asset': 'BTC', 'exchange': 'Bybit', 'funding_rate_pct': 0.00429, 'annualized_rate_pct': 4.7, 'next_funding_time': 1788825600000, 'sentiment': 'neutral'}, {'asset': 'BTC', 'exchange': 'Binance', 'funding_rate_pct': 0.002649, 'annualized_rate_pct': 2.9, 'next_funding_time': 1788825600000, 'sentiment': 'neutral'}, {'asset': 'BTC', 'exchange': 'OKX', 'funding_rate_pct': 0.001172, 'annualized_rate_pct': 1.28, 'next_funding_time': 1788854400000, 'sentiment': 'neutral'}], 'count': 3, 'sources': ['Binance', 'Bybit', 'OKX']}, 'open_interest': {'asset': 'BTC', 'price_usd': 79166.0, 'total_oi_usd': 8537086483.0, 'oi_change_1h_pct': -0.01, 'oi_change_24h_pct': 1.36, 'long_short_ratio': 1.177, 'exchanges': [{'exchange': 'Binance', 'oi_contracts': 107837.79, 'oi_change_1h_pct': -0.01, 'oi_change_24h_pct': 1.36, 'long_short_ratio': 1.177}, {'exchange': 'Bybit', 'oi_contracts': 54939.41, 'oi_change_5m_pct': 0.02}], 'source': 'binance/bybit'}}, 'checked_at': '2026-09-07T18:06:02Z'}
  verify : https://explorer.hiro.so/txid/0x30689b5ee9f779fe571f0b7797e2453b21cf7d220cadf4f1881586757347387f?chain=mainnet
```

`payment_logs` row for the txid: `uncertain` → `verified` → `payment_done`,
one delivery, no second payment.

---

## Receipt 2 — 2026-09-07 19:16 UTC

```
====================================================================
1) BUDGET-CAPPED SESSION  ->  sBTC PAYMENT ON STACKS MAINNET
====================================================================
payer (Stacks mainnet): SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5   gateway: https://agentpay.tools
session cap: $0.05   paying pre_trade_check({'symbol': 'BTC'}) in sBTC ...

  ✓ SETTLED
  RESULT : {'symbol': 'BTC', 'side': 'long', 'size_usd': 10000.0, 'verdict': 'ok', 'factors': {'liquidity': {'bucket_usd': 10000, 'slippage_pct': 0.0, 'spread_pct': 0.0, 'exchange': 'Binance', 'level': 'ok', 'reason': 'fills within 0.0% of best price'}, 'carry': {'median_funding_pct': 0.002228, 'annualized_pct': 2.44, 'venues': 3, 'level': 'ok', 'reason': 'carry unremarkable'}, 'crowding': {'long_short_ratio': 1.165, 'oi_change_24h_pct': 1.41, 'total_oi_usd': 8526754699.0, 'level': 'ok', 'reason': 'positioning unremarkable'}, 'security': {'level': 'n/a', 'reason': 'BTC is a native chain asset — no token contract to screen'}}, 'components': {'orderbook_depth': {'asset': 'BTC', 'pair': 'BTC/USDT', 'exchange': 'Binance', 'best_ask': 79175.5, 'best_bid': 79175.49, 'spread_pct': 0.0, 'depth': [{'notional_usd': 10000, 'slippage_pct': 0.0, 'executable': True}, {'notional_usd': 50000, 'slippage_pct': 0.0019, 'executable': True}, {'notional_usd': 250000, 'slippage_pct': None, 'executable': False}], 'exchanges': [{'exchange': 'Binance', 'best_ask': 79175.5, 'best_bid': 79175.49}, {'exchange': 'Bybit', 'best_ask': 79179.6, 'best_bid': 79179.5}], 'source': 'binance/bybit'}, 'funding_rates': {'asset': 'BTC', 'rates': [{'asset': 'BTC', 'exchange': 'Bybit', 'funding_rate_pct': 0.002676, 'annualized_rate_pct': 2.93, 'next_funding_time': 1788825600000, 'sentiment': 'neutral'}, {'asset': 'BTC', 'exchange': 'Binance', 'funding_rate_pct': 0.002228, 'annualized_rate_pct': 2.44, 'next_funding_time': 1788825600000, 'sentiment': 'neutral'}, {'asset': 'BTC', 'exchange': 'OKX', 'funding_rate_pct': -0.000538, 'annualized_rate_pct': -0.59, 'next_funding_time': 1788854400000, 'sentiment': 'neutral'}], 'count': 3, 'sources': ['Binance', 'Bybit', 'OKX']}, 'open_interest': {'asset': 'BTC', 'price_usd': 79155.0, 'total_oi_usd': 8526754699.0, 'oi_change_1h_pct': -0.13, 'oi_change_24h_pct': 1.41, 'long_short_ratio': 1.165, 'exchanges': [{'exchange': 'Binance', 'oi_contracts': 107722.25, 'oi_change_1h_pct': -0.13, 'oi_change_24h_pct': 1.41, 'long_short_ratio': 1.165}, {'exchange': 'Bybit', 'oi_contracts': 54850.8, 'oi_change_5m_pct': 0.0}], 'source': 'binance/bybit'}}, 'checked_at': '2026-09-07T19:16:04Z'}
  TX     : d1de1a792e2a23f5a0b22651eec4f3f00a49f3e9ca15c70c2a19fc8fb474dd8e
  NETWORK: stacks-mainnet
  RECEIPT: {'calls': 1, 'spent': '$0.01', 'remaining': '$0.04', 'budget': '$0.05', 'tools': ['pre_trade_check'], 'breakdown': [{'tool': 'pre_trade_check', 'cost': '$0.01', 'tx_hash': 'd1de1a792e2a23f5a0b22651eec4f3f00a49f3e9ca15c70c2a19fc8fb474dd8e', 'network': 'stacks-mainnet', 'success': True, 'state': 'settled'}]}
  verify : https://explorer.hiro.so/txid/0xd1de1a792e2a23f5a0b22651eec4f3f00a49f3e9ca15c70c2a19fc8fb474dd8e?chain=mainnet

====================================================================
2) PER-TOOL CAP BELOW PRICE  ->  REJECTED BEFORE ANY PAYMENT
====================================================================
session cap $0.05, per-tool cap $0.005 (< $0.01 price)   calling pre_trade_check ...
  ✓ rejected client-side — BudgetExceeded: Per-tool cap for 'pre_trade_check': this call ($0.01) would bring spend to $0.01, over the $0.005 cap
  RECEIPT: {'calls': 0, 'spent': '$0', 'remaining': '$0.05', 'budget': '$0.05', 'tools': [], 'breakdown': []}  (nothing spent)

Done. The sBTC txid above is the on-chain payment proof (Stacks mainnet).
```

---

## Receipt 3 — 2026-09-08 15:28 UTC

```
====================================================================
1) BUDGET-CAPPED SESSION  ->  sBTC PAYMENT ON STACKS MAINNET
====================================================================
payer (Stacks mainnet): SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5   gateway: https://agentpay.tools
session cap: $0.05   paying pre_trade_check({'symbol': 'BTC'}) in sBTC ...

  ✓ SETTLED
  RESULT : {'symbol': 'BTC', 'side': 'long', 'size_usd': 10000.0, 'verdict': 'ok', 'factors': {'liquidity': {'bucket_usd': 10000, 'slippage_pct': 0.0, 'spread_pct': 0.0, 'exchange': 'Binance', 'level': 'ok', 'reason': 'fills within 0.0% of best price'}, 'carry': {'median_funding_pct': 0.007023, 'annualized_pct': 7.69, 'venues': 3, 'level': 'ok', 'reason': 'carry unremarkable'}, 'crowding': {'long_short_ratio': 1.312, 'oi_change_24h_pct': 0.32, 'total_oi_usd': 8506555590.0, 'level': 'ok', 'reason': 'positioning unremarkable'}, 'security': {'level': 'n/a', 'reason': 'BTC is a native chain asset — no token contract to screen'}}, 'components': {'orderbook_depth': {'asset': 'BTC', 'pair': 'BTC/USDT', 'exchange': 'Binance', 'best_ask': 78493.17, 'best_bid': 78493.16, 'spread_pct': 0.0, 'depth': [{'notional_usd': 10000, 'slippage_pct': 0.0, 'executable': True}, {'notional_usd': 50000, 'slippage_pct': 0.0, 'executable': True}, {'notional_usd': 250000, 'slippage_pct': 0.0, 'executable': True}], 'exchanges': [{'exchange': 'Binance', 'best_ask': 78493.17, 'best_bid': 78493.16}, {'exchange': 'Bybit', 'best_ask': 78495.3, 'best_bid': 78495.2}], 'source': 'binance/bybit'}, 'funding_rates': {'asset': 'BTC', 'rates': [{'asset': 'BTC', 'exchange': 'Binance', 'funding_rate_pct': 0.009887, 'annualized_rate_pct': 10.83, 'next_funding_time': 1788883200000, 'sentiment': 'neutral'}, {'asset': 'BTC', 'exchange': 'OKX', 'funding_rate_pct': 0.007023, 'annualized_rate_pct': 7.69, 'next_funding_time': 1788912000000, 'sentiment': 'neutral'}, {'asset': 'BTC', 'exchange': 'Bybit', 'funding_rate_pct': 0.005396, 'annualized_rate_pct': 5.91, 'next_funding_time': 1788883200000, 'sentiment': 'neutral'}], 'count': 3, 'sources': ['Binance', 'Bybit', 'OKX']}, 'open_interest': {'asset': 'BTC', 'price_usd': 78481.0, 'total_oi_usd': 8506555590.0, 'oi_change_1h_pct': -0.44, 'oi_change_24h_pct': 0.32, 'long_short_ratio': 1.312, 'exchanges': [{'exchange': 'Binance', 'oi_contracts': 108390.0, 'oi_change_1h_pct': -0.44, 'oi_change_24h_pct': 0.32, 'long_short_ratio': 1.312}, {'exchange': 'Bybit', 'oi_contracts': 57028.83, 'oi_change_5m_pct': 0.15}], 'source': 'binance/bybit'}}, 'checked_at': '2026-09-08T15:28:24Z'}
  TX     : 59ce70146da57ffd8c71cd67eea9169373ad11b7298e77a88e5f946a0064e325
  NETWORK: stacks-mainnet
  RECEIPT: {'calls': 1, 'spent': '$0.01', 'remaining': '$0.04', 'budget': '$0.05', 'tools': ['pre_trade_check'], 'breakdown': [{'tool': 'pre_trade_check', 'cost': '$0.01', 'tx_hash': '59ce70146da57ffd8c71cd67eea9169373ad11b7298e77a88e5f946a0064e325', 'network': 'stacks-mainnet', 'success': True, 'state': 'settled'}]}
  verify : https://explorer.hiro.so/txid/0x59ce70146da57ffd8c71cd67eea9169373ad11b7298e77a88e5f946a0064e325?chain=mainnet

====================================================================
2) PER-TOOL CAP BELOW PRICE  ->  REJECTED BEFORE ANY PAYMENT
====================================================================
session cap $0.05, per-tool cap $0.005 (< $0.01 price)   calling pre_trade_check ...
  ✓ rejected client-side — BudgetExceeded: Per-tool cap for 'pre_trade_check': this call ($0.01) would bring spend to $0.01, over the $0.005 cap
  RECEIPT: {'calls': 0, 'spent': '$0', 'remaining': '$0.05', 'budget': '$0.05', 'tools': [], 'breakdown': []}  (nothing spent)

Done. The sBTC txid above is the on-chain payment proof (Stacks mainnet).
```
