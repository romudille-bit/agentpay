# AgentPay Roadmap

Last updated: April 26, 2026

---

## Engineering Hardening (post-DoraHacks judge feedback)

DoraHacks judges (April 2026) confirmed AgentPay's mainnet deployment is verifiable and called the budget-aware `Session` with hard spend caps "a genuinely useful primitive for agent safety." Score was held back by: no automated test coverage, in-memory-only state, incomplete code paths, and less architectural depth than projects with Soroban contracts or full SDK/middleware stacks. This track addresses each gap directly and frames the Instaward funding trajectory.

### Tier 1 — Cleanup ✅ Done April 25, 2026

Sub-30-minute fixes that removed the most visible "shipped without running it" signals.

- ✅ Fixed Base Mode B `rpc` → `rpc_url` NameError (`gateway/base.py:230`)
- ✅ Fixed `dex_liquidity` POST 404 — alias resolution applied to POST (`gateway/main.py:329`)
- ✅ Network reported correctly in Base payment receipts (`gateway/main.py:519-521`)
- ✅ `parse_payment_header(x_payment)["id"]` replaces brittle string split (`gateway/main.py:502`)
- ✅ Base advertised in HEAD pre-flight + `X-Payment-Required` header (`gateway/main.py:285-304`, `gateway/x402.py:90`)
- ✅ `BASE_NETWORK` default aligned with README (`gateway/config.py`)
- ✅ Empty `tx_hash` rejected in `parse_payment_header` (`gateway/x402.py:114`)
- ✅ Local working tree cleaned — `paper_trade_log.txt` and friends gone

### Tier 2 — Round 1 scope (May 2026): SDK + code improvements + landing

Round 1 leads with the dream feature (the SDK extension), pairs it with the production-grade hardening that removes the most embarrassing rough edges, and ships the user-friendly landing surface that's been blocked on the custom domain. Soroban settlement deferred to Round 2 — premature without users.

**D1 — Bounded-autonomy SDK + cost intelligence (Claude only):**

- `session.llm.claude(prompt, model)` — wraps Claude calls under the same `Session(max_spend=)` meter that already covers data calls. Two paths in one SDK:
  - **Production path**: dev brings their own `ANTHROPIC_API_KEY`; SDK calls Anthropic directly. Zero markup, zero proxy, zero capital risk.
  - **Demo path**: first 10 calls per anonymous user route through an AgentPay-hosted Haiku endpoint. Removes the "go create an Anthropic account first" onboarding friction. Costs us ~$0.005 per first-time user, capped at ~100 users/day.
- `Session.cost_breakdown()` — per-call ledger (tool, price, timestamp) for true P&L attribution across data + Claude calls.
- `Session.should_call(tool, expected_value_usd)` — turns the passive cap into an active decision surface.
- Hero demo `examples/weekend_bot.py` — bot bounded end-to-end by a single $20 budget covering both data and reasoning.
- Released as `agentpay-x402 v1.2.0` (PyPI) and `@romudille/agentpay-mcp v1.2.0` (npm).
- OpenAI integration deferred to Round 2.

**D2 — Code improvements:**

The fixes that make the gateway not embarrassing when devs poke at it. Each item closes a real bug or a measurable performance gap.

- **Pytest suite + GitHub Actions CI** — coverage on `gateway/x402.py`, `gateway/base.py`, `gateway/stellar.py` (happy + replay paths, mocked Horizon and JSON-RPC). Coverage badge in README. Closes the judge-cited #1 execution gap.
- **Refund / credit semantics on tool failure** — unify the three current failure modes that all silently keep the agent's money. Pick one rule, document it, enforce it.
- **Stellar facilitator fallback covers all non-200 cases** (not just 401). Plus flag-gate the OZ facilitator branch (`STELLAR_FACILITATOR_ENABLED=false` default) or remove it. **Removes the 15-second dead timeout currently sitting on every Stellar payment** — users feel this immediately.
- **Async safety** — wrap the three Stellar SDK call sites (`stellar.py:split_payment`, `main.py:_provision_wallet`, `main.py:get_usdc_balance`) in `asyncio.to_thread(...)`. Removes 5–10s event loop stalls under concurrency.
- **CDP Mode A schema validation** — sanity-check Base mainnet response shape before trusting `tx_hash` (`base.py:263-270`).

**D3 — Landing & user-friendly UI:**

Where users actually land. Currently `agentpay.tools` doesn't resolve and there's no landing surface — the biggest discoverability blocker we have.

- **Resolve `agentpay.tools`** — DNS + Railway settings. ~30 minutes of actual work but currently blocking everything else here.
- **Bazaar `outputSchema` fix** in `gateway/base.py:build_payment_required_header` — unlocks automatic indexing in Coinbase's Bazaar directory on next Base mainnet payment.
  - **Why both networks matter strategically**: Bazaar lives on Base, so it's our discovery channel inside the Coinbase ecosystem. But for AgentPay's $0.001–$0.005 tool prices, Stellar settles at ~$0.000001 per transaction vs. Base's ~$0.001–$0.01 per transaction in gas. **Discovery on Base, settlement on Stellar** — users find us through the Coinbase directory, then choose Stellar at runtime where the economics actually work for sub-cent micropayments.
- **Sponsored agent account integration** — wire the SDK / CLI to optionally bootstrap a Stellar wallet via the [stellar-sponsored-agent-account](https://github.com/oceans404/stellar-sponsored-agent-account) service (forked + self-hosted to remove dependency risk; ~$10/month in sponsor XLM). New users go from "acquire XLM, add USDC trustline, acquire USDC, then start" to "generate keypair, two HTTP calls, ready." Pairs with the Claude Haiku demo path in D1 — between them, a first-time user can call `session.llm.claude(...)` and `session.call("token_price", ...)` without holding any crypto or any account anywhere. ~1 day of work.
- **Landing page** at `agentpay.tools` — Segment 1 hero ("Run your trading bot on a fixed data budget"), 5-line code snippet, demo embed, testnet faucet CTA. One page, not a marketing site. Landing copy explicitly references the [Stellar Foundation's official x402 docs](https://developers.stellar.org/docs/build/agentic-payments/x402) and the [Foundation's x402 blog post](https://stellar.org/blog/foundation-news/x402-on-stellar) as positioning anchors — AgentPay is the production-grade implementation of the Foundation's published x402-MCP roadmap.
- **60-second weekend-bot demo video** — recorded once, embedded on landing, pinned to Twitter when the page goes live.
- **Brand basics** — refined logo, color palette, simple style guide PDF. Enough to feel intentional.

### Tier 3 — Round 2 candidate scope (June 2026)

Architectural depth + persistence + distribution + OpenAI integration.

- **Soroban escrow + atomic split contract on testnet** — receives the agent's USDC payment, splits 85/15 atomically, emits a `PaymentSettled` event the gateway listens for. Gateway becomes a relay, not a custodian. Closes the judges' "architectural depth / Soroban contracts" critique. Round 1 deferred this because the trust-the-gateway concern doesn't bite without users; Round 2 ships it once the user base from Round 1 makes it relevant.
- **Move replay state to Supabase**: `_completed_payments`, `_used_base_tx_hashes`, `_pending_challenges`, `_FAUCET_IP_LOG`. Replay protection on **both** `tx_hash` AND `payment_id`.
- **`payment_logs` audit trail** — row inserted before `split_payment(...)` + updated on result. Turns the fire-and-forget split into a queryable audit trail.
- **OpenAI integration** in the SDK — `session.llm.openai(prompt, model)`. Deferred from Round 1; revisit after Claude SDK gets real usage and we know what shape OpenAI's onboarding constraints will take.
- **Per-agent rate limiting** — keyed on `agent_address`, not IP (currently `slowapi` keys on IP).
- **Distribution sprint** — 10 tweets + 1 thread + 3 Discord intros + 20-dev outreach list targeting trading bot devs whose strategies combine cheap polling with selective premium calls.

### Tier 4 — Round 3 candidate scope (July 2026)

Soroban mainnet + advanced primitives.

- **Soroban contract mainnet rollout** — audit + per-payment-path migration from gateway-mediated split to contract-mediated split.
- **Budget escrow held in Soroban** — agent locks $20 in the contract; contract releases per call against a verified payment intent. Removes the need for the agent to hold balance per-tool-call.
- **Multi-agent budget pools** — one budget shared across N agent processes via the contract.
- **Lock down `/stats` and tighten CORS** (`allow_origins=["*"]` → documented frontends only).
- **Auth-gate or strip agent addresses** from public `/stats`.

### Tier 5 — Hygiene (continuous, no specific round)

Do once, never think about again.

- ✅ Split `gateway/main.py` (2,237 lines) into routes + services modules — done April 26.
- ✅ Dedupe `agent/wallet.py` vs `agentpay/_wallet.py` — done April 26.
- 🔜 README polish after Round 1 lands: CI badge, accurate Base section, accurate test instructions, link to deployed Soroban contract.
- 🔜 Replace silent `except: pass` patterns with logged warnings (`agent/wallet.py:295/308`, `stellar.py:281-282`).
- 🔜 `_FAUCET_COOLDOWN_SECS` documentation alignment — code says 600s, comment says 24 hours, docstring says "one wallet per IP per 24 hours." Pick a number.

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

**NEW `open_interest`** — ✅ Live on mainnet
- Total OI + 1h/24h change across Binance + Bybit
- Long/short ratio from Binance globalLongShortAccountRatio
- Testnet verified April 16, 2026 — mainnet deploy confirmed live April 21, 2026

**NEW `orderbook_depth`** — ✅ Live on mainnet
- Real bid/ask depth + slippage at $10k / $50k / $250k notional
- Binance primary, Bybit fallback; `exchange` param to override
- Testnet verified April 16, 2026 — mainnet deploy confirmed live April 21, 2026

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

**Custom domain** — `agentpay.tools` 🔜 still pending as of April 21, 2026
- Domain was available April 15 — still not resolving (ECONNREFUSED on agentpay.tools)
- Railway subdomain explicitly penalises quality score in Bazaar's ranking algorithm
- ~$10-15/year. Point CNAME to Railway service URL, add domain in Railway settings.
- Do before Bazaar registration

**Bazaar / x402 discovery indexing** 🔜 still pending as of April 21, 2026
- Verified: `outputSchema` is NOT yet in `base.py:build_payment_required_header`
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
| 402index.io | ✅ 14 tools registered |
| awesome-x402 | ✅ listed |
| Bazaar (Coinbase) | ❌ not indexed — needs custom domain + outputSchema fix |
| xpay.tools | 🔜 submission in progress |
| npm `@romudille/agentpay-mcp` | ✅ v1.0.3 |
