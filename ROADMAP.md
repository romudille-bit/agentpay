# AgentPay Roadmap

Last updated: April 16, 2026

---

## Current registry (14 tools live)

| Tool | Price | Status |
|------|-------|--------|
| `token_price` | $0.001 | Good — batch support + volume_24h pending (Week 2 remainder) |
| `token_market_data` | $0.001 | ✅ Renamed from `dex_liquidity`, repriced, description fixed |
| `gas_tracker` | $0.001 | Fair — Ethereum only, multi-chain pending (Week 2 remainder) |
| `fear_greed_index` | $0.001 | Fair — updates once/day, caching pending (Month 2) |
| `wallet_balance` | $0.002 | Good — USD valuation pending |
| `whale_activity` | $0.002 | ✅ Direction classification added (exchange inflow/outflow) |
| `defi_tvl` | $0.002 | Good — APY alongside TVL pending |
| `token_security` | $0.002 | Good — underrated, worth more promotion |
| `open_interest` | $0.002 | ✅ Live — Binance + Bybit, 1h/24h OI change, long/short ratio |
| `orderbook_depth` | $0.002 | ✅ Live — slippage at $10k/$50k/$250k, Binance + Bybit |
| `funding_rates` | $0.003 | Good — live, correctly priced |
| `dune_query` | $0.005 | ✅ fast_only mode added — usable for live bots now |
| `yield_scanner` | $0.004 | Good — live, niche (treasury agents > trading bots) |
| `crypto_news` | $0.003 | Weak — Reddit is lagging signal, overpriced. Reprice or replace (Month 2) |

---

## Week 1 — Done ✅

**`dex_liquidity` → `token_market_data`**
- Renamed in registry + Supabase, price $0.003 → $0.001
- Description corrected (no longer claims to return pool depth)
- `volume_change_24h_pct` now surfaced in response
- Legacy name still routed in gateway for backward compatibility

**`whale_activity` direction classification**
- Added `_EXCHANGE_WALLETS` dict — 33 addresses across Binance, Coinbase, Kraken, OKX, Bybit, Bitfinex, Gemini, Huobi, Gate.io
- Each transfer now includes `direction` (exchange_inflow / exchange_outflow / wallet_to_wallet) and `exchange_name`
- Response includes `direction_summary` counts
- Monthly scheduled audit created to find missing exchanges (runs 1st of each month)

**`dune_query` fast_only mode**
- New `fast_only` boolean param (default `False`)
- When `True`: returns cached result immediately or raises — never executes a fresh query
- Schema updated in registry + Supabase

---

## Week 2 — In Progress

Free public APIs, no new keys needed.

**NEW `open_interest`** — ✅ Done
- Total OI + 1h/24h change across Binance + Bybit
- Long/short ratio from Binance globalLongShortAccountRatio
- Testnet verified April 16, 2026 — mainnet deploy pending

**NEW `orderbook_depth`** — ✅ Done
- Real bid/ask depth + slippage at $10k / $50k / $250k notional
- Binance primary, Bybit fallback; `exchange` param to override
- Testnet verified April 16, 2026 — mainnet deploy pending

**FIX `token_price`** — batch + volume 🔜
- Add `volume_24h` to single-token response (already in CoinGecko payload)
- Add multi-symbol batch: one $0.002 call for up to 5 tokens
- CoinGecko `/simple/price` already accepts comma-separated ids — half a day

**FIX `gas_tracker`** — multi-chain 🔜
- Add `chain` param: arbitrum, base, polygon, optimism
- Same Etherscan V2 key, different `chainid` values (42161, 8453, 137, 10)
- Half a day

**The demo after Week 2:**
> "My bot read funding rates (+0.08%/8h on ETH), confirmed rising open interest (+12% in 24h), checked orderbook depth ($0.31% slippage on a $250k sell), and decided not to open the short. Total data cost: $0.006."

---

## Month 2 — On-chain intelligence

Requires paid APIs (~$80/month total if both). Prioritise by revenue.

**NEW `liquidation_heatmap`** — $0.003
- Recent liquidation volume + key price levels where large clusters sit
- Most-discussed signal in Freqtrade community
- Source: Coinglass liquidation API — free tier 100 req/day, paid ~$30/month
- Output: `{asset, recent_liquidations_1h_usd, liq_long_usd, liq_short_usd, key_levels[]}`
- Worth the cost — nothing in any competing registry surfaces this

**NEW `exchange_flows`** — $0.003
- Net BTC/ETH inflow/outflow to exchange wallets with directional attribution
- Upgrade from `whale_activity` into a true sell-pressure signal
- Start with Option B (free): Etherscan V2 + maintained list of ~500 known exchange hot wallets
- Upgrade to CryptoQuant (~$50/month) when revenue supports it
- Output: `{asset, net_flow_usd_24h, inflow_usd, outflow_usd, signal, top_exchanges[]}`

**FIX `fear_greed_index`** — cache + token sentiment
- Cache response for 6 hours in gateway memory (it updates once/day — stop re-fetching)
- Return `cache_age_hours` so bots can decide if data is fresh enough
- Optional: add per-token sentiment via Santiment free social volume tier

**FIX `crypto_news`** — demote or replace
- Option A (quick): rename to `community_sentiment`, drop price $0.003 → $0.001, be honest it's lagging
- Option B (better): replace Reddit with LunarCrush (X/Twitter + Reddit + news, free tier covers top 50 tokens)
- Option B is the right long-term call — build after `liquidation_heatmap`

---

## Backlog — Later

Lower urgency. Revisit when revenue supports API costs or specific user demand appears.

| Tool | Effort | Price | Notes |
|------|--------|-------|-------|
| `social_sentiment` | 2 days | $0.003 | Full LunarCrush replacement for `crypto_news`. Build after `liquidation_heatmap`. |
| `miner_flows` | 2 days | $0.002 | BTC miner outflows via CryptoQuant free tier. BTC-specific — build when BTC strategy users appear. |
| `wallet_balance` USD valuation | ½ day | — | Add `usd_value` per token + `usd_total` using internal `token_price` call |
| `defi_tvl` APY | ½ day | — | Add APY from DeFiLlama yields endpoint alongside TVL |

---

## Infrastructure

**Custom domain** — `agentpay.tools` (available as of April 15, 2026)
- Railway subdomain explicitly penalises quality score in Bazaar's ranking algorithm
- ~$10-15/year. Point CNAME to Railway service URL, add domain in Railway settings.
- Do before Bazaar registration

**Bazaar / x402 discovery indexing**
- Coinbase's CDP facilitator already handles Base payments (Mode A) — plumbing is there
- Missing: `outputSchema` in the `accepts` array of the `PAYMENT-REQUIRED` header
- Fix: update `base.py:build_payment_required_header` to embed tool `parameters` + `response_example` as `outputSchema`
- After fix + custom domain: one Base mainnet payment through CDP facilitator triggers automatic indexing
- No manual registration step

**Exchange wallet list maintenance**
- Scheduled task runs 1st of each month
- Outputs audit report to `exchange_wallet_audit.md` for manual review
- Currently missing: Bitget, MEXC, Crypto.com, KuCoin, Bithumb, HTX

---

## Discovery directories

| Directory | Status |
|-----------|--------|
| x402scout | ✅ indexed, health-checked every 15min |
| Glama MCP | ✅ listed |
| 402index.io | ✅ 12 tools registered |
| awesome-x402 | ✅ listed |
| Bazaar (Coinbase) | ❌ not indexed — needs custom domain + outputSchema fix |
| xpay.tools | 🔜 submission in progress |
| npm `@romudille/agentpay-mcp` | ✅ v1.0.3 |
