# AgentPay — MCP Tool Payment Gateway

The economic layer for AI agents. Pay-per-call MCP tools via x402 on Stellar.

## Project Structure

```
agentpay/
├── gateway/          # x402 payment gateway server (FastAPI)
│   ├── main.py       # Main server — run this
│   ├── x402.py       # x402 payment handler logic
│   ├── stellar.py    # Stellar wallet + payment verification
│   └── config.py     # Environment config
├── tools/            # MCP tool implementations
│   ├── token_price.py
│   ├── wallet_balance.py
│   ├── dex_liquidity.py
│   └── gas_tracker.py
├── registry/         # Tool registry (database layer)
│   ├── models.py
│   └── registry.py
├── agent/            # Example agent that uses paid tools
│   ├── agent.py      # LangGraph agent
│   └── wallet.py     # Agent Stellar wallet helper
├── setup_wallet.py   # One-time wallet setup script
├── requirements.txt
└── .env.example
```

## Quick Start (5 steps)

### Step 1 — Install dependencies
```bash
pip install -r requirements.txt
```

### Step 2 — Set up Stellar testnet wallet
```bash
python setup_wallet.py
```
This creates two wallets (gateway + test agent) and funds them from the testnet faucet.
Copy the output into your `.env` file.

### Step 3 — Configure environment
```bash
cp .env.example .env
# Edit .env with your wallet keys from Step 2
```

### Step 4 — Start the gateway
```bash
cd gateway
uvicorn main:app --reload --port 8000
```

### Step 5 — Run the test agent
```bash
cd agent
python agent.py
```

Watch the agent call tools and pay automatically in real time.

## How It Works

```
Agent calls tool endpoint
        ↓
Gateway returns HTTP 402 + Stellar address + price
        ↓
Agent sends USDC payment on Stellar testnet
        ↓
Gateway verifies payment on-chain
        ↓
Gateway calls the real MCP tool
        ↓
Returns result to agent
```

## Available Tools (MVP)

| Tool | Price | Description |
|------|-------|-------------|
| token_price | $0.001 | Live crypto token price |
| wallet_balance | $0.002 | Stellar/ETH wallet balance |
| dex_liquidity | $0.003 | DEX liquidity for a token pair |
| gas_tracker | $0.001 | Current ETH gas prices |

## Revenue Split

- Tool developer: 85%
- AgentPay gateway: 15%

All splits happen automatically on-chain via Stellar payments.

## Deploying to Production

- Backend: Railway (https://railway.app) — free tier works
- Database: Supabase (https://supabase.com) — free tier works  
- Frontend: Vercel (https://vercel.com) — free tier works
- Switch `STELLAR_NETWORK=mainnet` in .env when ready
