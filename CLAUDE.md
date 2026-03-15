# AgentPay

x402 payment gateway for AI agents. Agents pay USDC on Stellar to call data tools.

## Architecture

```
agent/ → gateway (port 8001) → registry → real APIs
            ↕ x402 flow
         Stellar testnet
```

**Flow**: Agent calls tool → 402 challenge → agent pays USDC on Stellar → retries with `X-Payment` header → gateway verifies tx → returns data.

## Key Files

| File | Purpose |
|------|---------|
| `gateway/main.py` | FastAPI gateway, x402 flow, real API calls |
| `gateway/stellar.py` | Stellar payment verification |
| `gateway/config.py` | pydantic-settings (reads .env) |
| `registry/registry.py` | In-memory tool registry (6 tools) |
| `agent/wallet.py` | `AgentWallet` + `Session` (budget cap, fallback) |
| `agent/agent.py` | `AgentPayClient` (low-level x402 HTTP client) |
| `agent/budget_demo.py` | Full 5-tool ETH analysis demo |

## Tools & Prices

| Tool | Price | API |
|------|-------|-----|
| `token_price` | $0.001 | CoinGecko |
| `gas_tracker` | $0.001 | Etherscan V2 |
| `wallet_balance` | $0.002 | Stellar Horizon / Etherscan V2 |
| `whale_activity` | $0.002 | Etherscan V2 |
| `dex_liquidity` | $0.003 | CoinGecko |
| `dune_query` | $0.005 | Dune Analytics |

## How to Run

```bash
source venv/bin/activate

# Gateway (port 8001)
uvicorn gateway.main:app --port 8001 --reload

# Agent demo
python agent/budget_demo.py
python agent/agent.py
```

## Production

**Gateway URL**: `https://gateway-production-2cc2.up.railway.app`
**Deploy**: `railway up --service gateway` (Railway project: `agentpay`)

## Wallets (testnet)

- Gateway: `GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S`
- Test agent: `GBLYTV4ZME4CARIUVG2WC4LWQUB7HQVZ5W6IZNXLYEMTUYNX2QYOUMU7`

## Required .env Keys

```
GATEWAY_SECRET_KEY, GATEWAY_PUBLIC_KEY, STELLAR_NETWORK
TEST_AGENT_SECRET_KEY, DUNE_API_KEY, ETHERSCAN_API_KEY
COINGECKO_API_URL, AGENTPAY_GATEWAY_URL
```

## Agent Session API

```python
from agent.wallet import AgentWallet, Session, BudgetExceeded

wallet = AgentWallet(secret_key=SECRET, network="testnet")
with Session(wallet, gateway_url=GATEWAY, max_spend="0.10") as session:
    result = session.call("token_price", {"symbol": "ETH"})
    print(session.spent(), session.remaining())
```
