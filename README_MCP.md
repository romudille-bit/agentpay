# AgentPay MCP Server

Give Claude Desktop direct access to live crypto data — prices, gas, whale activity, DeFi TVL, and more. 17 tools are free. No API key, no wallet, no setup required.

**18 tools. 17 free. No API keys needed.**

---

## Quickstart

```bash
npx @romudille/agentpay-mcp
```

Or configure manually in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agentpay": {
      "command": "python",
      "args": ["/absolute/path/to/agentpay/gateway/mcp_server.py"],
      "env": {
        "AGENTPAY_GATEWAY_URL": "https://agentpay.tools"
      }
    }
  }
}
```

Restart Claude Desktop. Try asking:

- *"What's the current ETH price and 24h change?"*
- *"Check the Fear & Greed Index"*
- *"What's the Ethereum gas price right now?"*
- *"Show me DeFi TVL for Aave"*
- *"Any large whale transfers for USDC in the last hour?"*

---

## Available Tools

| Tool | Price | What it does |
|------|-------|-------------|
| `url_reader` | Free | Convert any URL to clean, LLM-ready markdown |
| `web_search` | Free | Top 5 search results with full content |
| `market_snapshot` | Free | S&P 500, BTC, ETH, and gas in one call |
| `token_price` | Free | Current USD price, 24h change, market cap |
| `gas_tracker` | Free | Ethereum gas prices (slow/standard/fast gwei) |
| `fear_greed_index` | Free | Crypto Fear & Greed Index (0–100) with history |
| `token_market_data` | Free | 24h volume, market cap, ATH for a token pair |
| `wallet_balance` | Free | Token balances for any Ethereum or Stellar address |
| `whale_activity` | Free | Large transfers for a token (≥$100k by default) |
| `defi_tvl` | Free | DeFi protocol TVL from DeFiLlama |
| `token_security` | Free | Honeypot, rug pull, and security scan for any token contract |
| `open_interest` | Free | Total OI + 1h/24h change + long/short ratio across Binance + Bybit |
| `orderbook_depth` | Free | Best bid/ask + slippage at $10k/$50k/$250k notional |
| `funding_rates` | Free | Perp funding rates across Binance, Bybit, and OKX |
| `crypto_news` | Free | Latest headlines and community sentiment from Reddit |
| `yield_scanner` | Free | Best DeFi yield opportunities for a token across protocols |
| `dune_query` | Free | Run any Dune Analytics query by ID |
| `session_create` | $0.001 | Open a budget-capped agent session with a hard spend cap |

---

## Paid tool: session_create

`session_create` is the only paid tool ($0.001 USDC per session). It opens a budget-capped AgentPay session with a hard `max_spend` limit — for autonomous agents that need to enforce a spend ceiling across multiple tool calls.

To use it, you'll need a wallet funded with USDC on Stellar or Base.

**Testnet (free, instant):**

```bash
curl https://gateway-testnet-production.up.railway.app/faucet
```

Save the `secret_key` from the response, then add `STELLAR_SECRET_KEY` to your MCP env config and set `AGENTPAY_GATEWAY_URL` to the testnet URL.

**Mainnet:** Fund a Stellar wallet via [Coinbase](https://coinbase.com) or [Lobstr](https://lobstr.co).

---

## Environment variables

| Variable | Required | Default | When needed |
|----------|----------|---------|-------------|
| `AGENTPAY_GATEWAY_URL` | No | `https://agentpay.tools` | Always |
| `STELLAR_SECRET_KEY` | No | — | Only for `session_create` |

---

## Troubleshooting

**Tools don't appear in Claude** — Restart Claude Desktop after editing the config. Check logs in `~/Library/Logs/Claude/` (macOS) or `%APPDATA%\Claude\logs\` (Windows).

**"STELLAR_SECRET_KEY is not set"** — Only needed for `session_create`. Add your key to the `env` block in `claude_desktop_config.json`.

**"InsufficientFunds"** — Only applies to `session_create`. For testnet: run the faucet command again. For mainnet: fund your Stellar wallet with USDC via Coinbase or Lobstr.

**Payment verification failed** — This usually means the Stellar transaction memo was truncated. File an issue on GitHub.
