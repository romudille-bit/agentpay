# @romudille/agentpay-mcp

AgentPay MCP server — 12 crypto data tools for AI agents.
Pay per call in USDC on Stellar or Base. No API keys.

## Quick Start (zero config)

Add to Claude Desktop config:

```json
{
  "mcpServers": {
    "agentpay": {
      "command": "npx",
      "args": ["@romudille/agentpay-mcp"]
    }
  }
}
```

A testnet Stellar wallet is created and funded automatically
on first run.

## Options

**Option A — Auto (testnet Stellar wallet, free):**
No config needed. Wallet created automatically.

**Option B — Your Stellar wallet (testnet or mainnet):**
```json
{
  "env": {
    "STELLAR_SECRET_KEY": "S...",
    "STELLAR_NETWORK": "testnet"
  }
}
```

**Option C — Base mainnet (real USDC):**
```json
{
  "env": {
    "BASE_PRIVATE_KEY": "0x..."
  }
}
```

## Tools (12)

| Tool | Price |
|------|-------|
| `token_price` | $0.001 |
| `gas_tracker` | $0.001 |
| `fear_greed_index` | $0.001 |
| `wallet_balance` | $0.002 |
| `whale_activity` | $0.002 |
| `defi_tvl` | $0.002 |
| `token_security` | $0.002 |
| `dex_liquidity` | $0.003 |
| `crypto_news` | $0.003 |
| `funding_rates` | $0.003 |
| `yield_scanner` | $0.004 |
| `dune_query` | $0.005 |

Gateway: https://gateway-production-2cc2.up.railway.app
GitHub: https://github.com/romudille-bit/agentpay
