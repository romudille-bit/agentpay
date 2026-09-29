# AgentPay MCP Server

**The economic intelligence layer for AI agents.** 20 tools (17 free, 3 paid at $0.01: session_create, pre_trade_check, verified_route). No API keys, no wallet, no setup. The Node MCP mints a per-install wallet identity on first run (spending OFF unless `AGENTPAY_ENABLE_PAID=1`) and exposes a free `verified_route` trust preview + `estimate_plan`.

Agents call tools within a hard budget cap, pay USDC on-chain only when tools cost money, and get a full session receipt — every call, every cost, every decision.

---

## Quickstart — Python SDK (3 lines, zero setup)

```python
from agentpay import quickstart

s = quickstart()                                    # registers + mints wallet
r = s.call("token_price", {"symbol": "ETH"})
print(r.data["price_usd"])                         # $3,421.05
print(s.spending_summary())                        # receipt: every call, cost, tx
```

Install: `pip install agentpay-x402`

Set a hard budget or bring your own wallet:

```python
s = quickstart(max_spend=0.10)                     # hard cap at $0.10
s = quickstart(secret_key="S...", base_key="0x...") # your Stellar + Base wallet
```

Every call is session-tracked. The cap is enforced **before** any payment is signed.

---

## Quickstart — MCP server (Claude Desktop / any MCP runtime)

```bash
npx @romudille/agentpay-mcp
```

Or configure manually in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agentpay": {
      "command": "npx",
      "args": ["-y", "@romudille/agentpay-mcp"]
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
| `session_create` | $0.01 | Open a budget-capped agent session with a hard spend cap |
| `pre_trade_check` | $0.01 | One-call trade verdict: slippage at size + funding carry + OI crowding + security |
| `verified_route` | $0.01 | Buyer-side trust oracle: the vetted x402 provider for a need, ready to pay (free preview keyless) |

---

## Paid tools (wallet mode)

The three paid tools settle in-place, gasless EIP-3009 on Base — no ETH needed, and a rejected
call moves no USDC. Spending is off until you enable it; every paid call counts against
`AGENTPAY_MAX_SPEND` and calls past the cap are refused before signing.

```json
{
  "mcpServers": {
    "agentpay": {
      "command": "npx",
      "args": ["-y", "@romudille/agentpay-mcp"],
      "env": { "AGENTPAY_ENABLE_PAID": "1", "AGENTPAY_MAX_SPEND": "0.10" }
    }
  }
}
```

`AGENTPAY_ENABLE_PAID=1` spends from the per-install wallet the MCP mints on first run
(`~/.agentpay/mcp-wallet.json`; address printed on stderr at startup) — fund it with a few
cents of USDC on Base mainnet. Or set `AGENTPAY_BASE_KEY` to bring your own EVM key. Use a
dedicated, small-balance key: the cap is your blast radius.

---

## Recipe — pre-trade guardrail alongside Coinbase for Agents

Coinbase for Agents gives an agent trading (`orders_preview` / `orders_create`) and a curated
x402 data catalog. AgentPay adds the step that catalog doesn't have: a one-call risk verdict
*before* the order. Run the two MCPs side by side — AgentPay is not part of Coinbase's catalog
and settles its own $0.01 from the wallet above.

Prompt to paste into the session:

> Trading guardrail: before every Coinbase orders_create, call AgentPay pre_trade_check with {"symbol": <asset>, "side": <long|short>, "size_usd": <notional>}. Proceed only if the verdict is "ok". On "caution" or "avoid", show me the per-factor reasons and ask before doing anything else. For data the Coinbase catalog does not cover, call AgentPay verified_route first and use the provider it picks. Never exceed AGENTPAY_MAX_SPEND.

The verdict comes back as `ok` / `caution` / `avoid` with a per-factor breakdown and the raw
components embedded, so the agent can show its reasons instead of just refusing.

---

## Environment variables

| Variable | Default | When needed |
|----------|---------|-------------|
| `AGENTPAY_GATEWAY_URL` | `https://agentpay.tools` | Point at a different gateway |
| `AGENTPAY_ENABLE_PAID` | *(unset — off)* | `1` to settle paid tools from the per-install wallet |
| `AGENTPAY_BASE_KEY` | *(unset)* | Bring-your-own EVM key; implies paid mode |
| `AGENTPAY_WALLET_PATH` | `~/.agentpay/mcp-wallet.json` | Where the minted wallet lives |
| `AGENTPAY_MAX_SPEND` | `0.10` | Hard session spend cap in USDC (paid mode) |

---

## Troubleshooting

**Tools don't appear in Claude** — Restart Claude Desktop after editing the config. Check logs in `~/Library/Logs/Claude/` (macOS) or `%APPDATA%\Claude\logs\` (Windows).

**"is a paid tool … this MCP is running keyless"** — Expected until you enable paid mode: set `AGENTPAY_ENABLE_PAID=1` (and fund the printed address) or `AGENTPAY_BASE_KEY`.

**Call refused by the cap** — The session has reached `AGENTPAY_MAX_SPEND`. Raise it deliberately, or restart the MCP for a fresh session.

**Payment verification failed** — The gateway rejected the signed authorization (wrong amount, expired, or replay). Retry once; if it persists, file an issue on GitHub.
