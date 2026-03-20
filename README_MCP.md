# AgentPay MCP Server

Give Claude Desktop direct access to live crypto data — prices, gas, whale activity, DeFi TVL, and more. Payments happen automatically in USDC on Stellar.

**9 tools. $0.001–$0.005 per call. No API keys needed.**

---

## Quickstart

### 1. Get a funded test wallet (one command)

```bash
curl https://gateway-production-2cc2.up.railway.app/faucet
```

Save the `secret_key` from the response — you'll use it in step 3.

---

### 2. Install dependencies

```bash
pip install mcp stellar-sdk httpx
```

---

### 3. Configure Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "agentpay": {
      "command": "python",
      "args": ["/absolute/path/to/agentpay/gateway/mcp_server.py"],
      "env": {
        "STELLAR_SECRET_KEY": "S...",
        "AGENTPAY_GATEWAY_URL": "https://gateway-production-2cc2.up.railway.app"
      }
    }
  }
}
```

Replace `/absolute/path/to/agentpay` with the real path and `S...` with your Stellar secret key.

---

### 4. Restart Claude Desktop

The AgentPay tools will appear in the tools panel. Try asking:

- *"What's the current ETH price and 24h change?"*
- *"Check the Fear & Greed Index"*
- *"What's the Ethereum gas price right now?"*
- *"Show me DeFi TVL for Aave"*
- *"Any large whale transfers for USDC in the last hour?"*

---

## Available Tools

| Tool | Price | What it does |
|------|-------|-------------|
| `token_price` | $0.001 | Current USD price, 24h change, market cap |
| `gas_tracker` | $0.001 | Ethereum gas prices (slow/standard/fast gwei) |
| `fear_greed_index` | $0.001 | Crypto Fear & Greed Index (0–100) with history |
| `wallet_balance` | $0.002 | Token balances for any Ethereum or Stellar address |
| `whale_activity` | $0.002 | Large transfers for a token (≥$100k by default) |
| `defi_tvl` | $0.002 | DeFi protocol TVL from DeFiLlama |
| `dex_liquidity` | $0.003 | 24h volume, market cap, ATH for a token pair |
| `crypto_news` | $0.003 | Latest headlines and community sentiment |
| `dune_query` | $0.005 | Run any Dune Analytics query by ID |

---

## How payments work

Each tool call triggers the x402 payment flow automatically:

1. Server POSTs to the AgentPay gateway → receives a `402` challenge
2. Sends USDC on Stellar (~2–3 seconds on testnet)
3. Retries with `X-Payment` header → receives data

You never see this — it just works. Costs are shown in each tool response:

```
[Paid $0.001 USDC | tx: 05867b331f578b15...]
```

---

## Environment variables

| Variable | Required | Default |
|----------|----------|---------|
| `STELLAR_SECRET_KEY` | Yes | — |
| `AGENTPAY_GATEWAY_URL` | No | `https://gateway-production-2cc2.up.railway.app` |

---

## Using on mainnet

Change `STELLAR_NETWORK` to `mainnet` (the gateway detects this from the challenge response) and fund your wallet with real USDC on Stellar.

---

## Troubleshooting

**"STELLAR_SECRET_KEY is not set"** — Add your key to the `env` block in `claude_desktop_config.json`.

**"InsufficientFunds"** — Your testnet wallet is empty. Run `curl .../faucet` again or send USDC from another wallet.

**Tools don't appear in Claude** — Restart Claude Desktop after editing the config. Check the MCP logs in `~/Library/Logs/Claude/`.

**Payment verification failed** — This usually means the Stellar transaction memo was truncated. File an issue on GitHub.
