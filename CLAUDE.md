# AgentPay

x402 payment gateway for AI agents. Agents pay USDC on Stellar (mainnet) or Base (mainnet) to call data tools.

## Architecture

```
agent/ → gateway (Railway) → registry → real APIs
              ↕ x402 flow
         Stellar mainnet OR Base mainnet
```

**Flow**: Agent calls tool → 402 challenge → agent pays USDC on Stellar or Base → retries with `X-Payment` header → gateway verifies tx on-chain → returns data + splits 85% to developer wallet.

## Two Live Services (Railway)

| Service | URL | Network |
|---------|-----|---------|
| `gateway` (production) | `https://gateway-production-2cc2.up.railway.app` | Stellar mainnet + Base mainnet |
| `gateway-testnet` | `https://gateway-testnet-production.up.railway.app` | Stellar testnet (faucet enabled) |

## Key Files

| File | Purpose |
|------|---------|
| `gateway/main.py` | FastAPI gateway, x402 flow, real API calls, keepalive ping |
| `gateway/stellar.py` | Stellar payment verification via Horizon |
| `gateway/base.py` | Base mainnet payment verification via JSON-RPC |
| `gateway/config.py` | pydantic-settings (reads .env, extra="ignore") |
| `registry/registry.py` | In-memory tool registry (12 tools, response_example per tool) |
| `gateway/mcp_server.py` | MCP stdio server — exposes all 12 tools |
| `agent/wallet.py` | `AgentWallet` + `Session` (budget cap, fallback) |
| `agent/agent.py` | `AgentPayClient` (low-level x402 HTTP client) |
| `agent/budget_demo.py` | Full 5-tool ETH analysis demo |
| `npm/bin/agentpay-mcp.js` | npm MCP wrapper — checks network before faucet |

## Tools & Prices (12 live tools)

| Tool | Price | API | Category |
|------|-------|-----|----------|
| `token_price` | $0.001 | CoinGecko | data |
| `gas_tracker` | $0.001 | Etherscan V2 | data |
| `fear_greed_index` | $0.001 | alternative.me | data |
| `wallet_balance` | $0.002 | Stellar Horizon / Etherscan V2 | data |
| `whale_activity` | $0.002 | Etherscan V2 | monitoring |
| `defi_tvl` | $0.002 | DeFiLlama | defi |
| `token_security` | $0.002 | GoPlus Security API | security |
| `dex_liquidity` | $0.003 | CoinGecko | defi |
| `crypto_news` | $0.003 | Reddit r/CryptoCurrency | data |
| `funding_rates` | $0.003 | Binance + Bybit + OKX | defi |
| `yield_scanner` | $0.004 | DeFiLlama yields | defi |
| `dune_query` | $0.005 | Dune Analytics | data |

## Wallets

| Role | Network | Public Key |
|------|---------|------------|
| Gateway | Stellar mainnet | `GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2` |
| Gateway | Stellar testnet | `GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S` |
| Gateway | Base mainnet | `0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7` |
| Test agent | Stellar mainnet | `GBCVQCNFWPM3GDO4GPT4YEQ42ZHPY67QTJA3WN5ERQIKQDXKBX62SLNJ` |
| Test agent | Stellar testnet | `GBLYTV4ZME4CARIUVG2WC4LWQUB7HQVZ5W6IZNXLYEMTUYNX2QYOUMU7` |

USDC issuer mainnet: `GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN`
USDC issuer testnet: `GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5`
USDC Base mainnet: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`

## How to Run

```bash
cd /Users/velvetvau/Downloads/agentpay
source venv/bin/activate

# Gateway local (port 8001)
uvicorn gateway.main:app --port 8001 --reload

# Agent demo against production mainnet
python3 - << 'PYEOF'
from agent.wallet import AgentWallet, Session
wallet = AgentWallet(secret_key="S...", network="mainnet")
with Session(wallet, gateway_url="https://gateway-production-2cc2.up.railway.app", max_spend="0.10") as session:
    r = session.call("token_price", {"symbol": "ETH"})
    print(r["result"]["price_usd"])
PYEOF
```

## Deploy

```bash
# Production (mainnet)
railway up --service gateway

# Testnet
railway up --service gateway-testnet
```

## Revenue Split

Gateway takes 15% (`GATEWAY_FEE_PERCENT=0.15`). 85% auto-splits to `developer_address` per tool on every payment. Currently all 12 tools point to the mainnet gateway wallet.

## Agent Session API

```python
from agent.wallet import AgentWallet, Session, BudgetExceeded

wallet = AgentWallet(secret_key=SECRET, network="mainnet")
with Session(wallet, gateway_url=GATEWAY, max_spend="0.10") as session:
    result = session.call("token_price", {"symbol": "ETH"})
    price = result["result"]["price_usd"]  # Note: data is in result["result"]
    print(session.spent(), session.remaining())
```

## npm / MCP

```bash
npx @romudille/agentpay-mcp  # auto-checks network, no faucet on mainnet
```

MCP listed on Glama: https://glama.ai/mcp/servers/romudille-bit/agentpay

## Discovery

| Directory | Status |
|-----------|--------|
| x402scout | ✅ indexed, health-checked every 15min |
| Glama MCP | ✅ listed |
| 402index.io | ✅ 12 tools registered |
| awesome-x402 | ✅ listed |
| xpay.tools | 🔜 submission in progress |
| npm | ✅ @romudille/agentpay-mcp v1.0.3 |

## Well-Known Endpoints

- `/.well-known/agentpay.json` — AgentPay manifest
- `/.well-known/agent.json` — A2A agent card
- `/.well-known/l402-services` — 402index.io discovery format
- `/sitemap.xml` — 17-URL sitemap
- `/llms.txt` — LLM-readable service description (tools + integration guide)

## Hackathon

Submitted to Stellar Hacks (DoraHacks) — deadline April 13, 2026.
URL: https://dorahacks.io/hackathon/stellar-agents-x402-stripe-mpp/detail
