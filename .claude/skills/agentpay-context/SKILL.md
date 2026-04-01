# AgentPay — Full Project Context

## What It Is

AgentPay is a live x402 payment gateway on **Stellar mainnet + Base mainnet** that lets AI agents autonomously pay for crypto data tools using USDC. Agents discover tools, receive HTTP 402 payment challenges, pay on-chain, then get real data back — all within a hard budget cap.

**Business model**: Gateway takes 15% of each payment (GATEWAY_FEE_PERCENT=0.15), forwarding 85% to the tool developer's Stellar wallet automatically via split_payment().

**Status**: Live in production on Stellar mainnet as of March 31, 2026. First real mainnet payment confirmed: tx 29f59465cfed5620 on Stellar mainnet.

---

## Two Railway Services

| Service | URL | Network | Faucet |
|---------|-----|---------|--------|
| gateway | https://gateway-production-2cc2.up.railway.app | Stellar mainnet + Base mainnet | No — returns 404 |
| gateway-testnet | https://gateway-testnet-production.up.railway.app | Stellar testnet | Yes — enabled |

---

## x402 Payment Protocol

1. Agent POSTs to /tools/{name}/call with {parameters, agent_address}
2. Gateway returns 402 with {payment_id, amount_usdc, pay_to, instructions}
3. Agent sends USDC on Stellar (memo = payment_id[:28]) OR Base (ERC-20 transfer)
4. Agent retries with header: X-Payment: tx_hash=<hash>,from=<address>,id=<payment_id>
5. Gateway calls verify_payment() on Stellar Horizon or Base JSON-RPC
6. Gateway calls the real API, returns data, auto-splits 85% to developer_address

**Memo match logic** (gateway/stellar.py): payment_id.startswith(memo) or memo.startswith(payment_id)

**Response structure**: All tool data is wrapped — access via result["result"], not result directly.

---

## Project Structure

```
agentpay/
├── gateway/
│   ├── main.py          # FastAPI app, x402 flow, real API dispatchers, keepalive
│   ├── stellar.py       # Stellar payment verification + split_payment()
│   ├── base.py          # Base mainnet payment verification via JSON-RPC
│   ├── config.py        # pydantic-settings BaseSettings (extra="ignore")
│   └── x402.py          # x402 protocol helpers
├── registry/
│   └── registry.py      # 12-tool registry with response_example per tool
├── agent/
│   ├── wallet.py        # AgentWallet (Stellar SDK) + Session (budget manager)
│   ├── agent.py         # AgentPayClient (low-level x402 HTTP client)
│   └── budget_demo.py   # 5-tool ETH analysis demo
├── npm/
│   └── bin/
│       └── agentpay-mcp.js  # npm MCP wrapper, checks /health before faucet
├── railway.toml         # Railway deploy config
├── requirements.txt
├── CLAUDE.md            # Quick reference
└── .env                 # All secrets (gitignored, never committed)
```

---

## Tools Registry (12 tools)

| Tool | Price | Category | Real API |
|------|-------|----------|----------|
| token_price | $0.001 | data | CoinGecko /simple/price |
| gas_tracker | $0.001 | data | Etherscan V2 gasoracle |
| fear_greed_index | $0.001 | data | alternative.me/fng |
| wallet_balance | $0.002 | data | Stellar Horizon / Etherscan V2 |
| whale_activity | $0.002 | monitoring | Etherscan V2 tokentx |
| defi_tvl | $0.002 | defi | DeFiLlama api.llama.fi |
| token_security | $0.002 | security | GoPlus Security API |
| dex_liquidity | $0.003 | defi | CoinGecko /coins/{id} |
| crypto_news | $0.003 | data | Reddit r/CryptoCurrency |
| funding_rates | $0.003 | defi | Binance + Bybit + OKX public APIs |
| yield_scanner | $0.004 | defi | DeFiLlama yields (18k+ pools) |
| dune_query | $0.005 | data | Dune Analytics API |

All developer_address values point to mainnet gateway wallet GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2.

---

## Wallets

| Role | Network | Public Key |
|------|---------|------------|
| Gateway | Stellar mainnet | GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2 |
| Gateway | Stellar testnet | GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S |
| Gateway | Base mainnet | 0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7 |
| Test agent | Stellar mainnet | GBCVQCNFWPM3GDO4GPT4YEQ42ZHPY67QTJA3WN5ERQIKQDXKBX62SLNJ |
| Test agent | Stellar testnet | GBLYTV4ZME4CARIUVG2WC4LWQUB7HQVZ5W6IZNXLYEMTUYNX2QYOUMU7 |

USDC issuer mainnet: GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN
USDC issuer testnet: GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5
USDC Base contract: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

---

## Known Issues & Fixes

| Problem | Fix |
|---------|-----|
| Memo too strict | payment_id.startswith(memo) or memo.startswith(payment_id) |
| pydantic-settings rejects extra vars | extra = "ignore" in Settings.Config |
| Etherscan V1 deprecated | Use V2 API + chainid=1 |
| Railway cold start | Background keepalive ping every 5min |
| HEAD / returning 405 | Added HEAD method to root endpoint |
| Faucet breaks on mainnet | Gated: if mainnet return 404 |
| Supabase RLS disabled | RLS enabled, public SELECT only |
| developer_address pointed to testnet | Updated all 12 rows in Supabase + registry.py |
| npm auto-wallet on mainnet | Checks /health before /faucet |
| revenue split going to testnet wallet | Supabase + registry.py updated to mainnet address |

---

## Hackathon

Submitted to Stellar Hacks (DoraHacks) — deadline April 13, 2026.
URL: https://dorahacks.io/hackathon/stellar-agents-x402-stripe-mpp/detail
Covers 7 hackathon use cases: financial market data, trading signals, security scanning,
real-time news, blockchain indexing, agent service discovery, Bazaar-style discoverability.
