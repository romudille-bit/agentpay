# @romudille/agentpay-mcp

**The economic-intelligence layer for AI agents.** Most agent-payment tools are a wallet —
they move money. AgentPay is the layer that decides whether to spend it at all: a hard
budget cap enforced at the payment layer, cost awareness before every call, and a
verifiable receipt after.

Self-contained Node MCP server (Node ≥ 18). No Python, no repo, no wallet, no API keys.
17 free tools work out of the box, plus **`verified_route`** — a keyless buyer-side *trust
preview* that vets the x402 marketplace (sweep → drop stubs & sybil factories → rank by real
unique-payer usage) and names the real, used provider for your need. It withholds the
ready-to-pay payload by design; the full multi-query sweep + ready-to-pay challenge come from
the paid `verified_route` ($0.01) via the `agentpay-x402` SDK. (`route` is kept as a legacy
alias; `estimate_plan` prices a multi-tool plan before you spend.)

Gateway: `https://agentpay.tools`

## Quick Start (zero config)

```bash
npx -y @romudille/agentpay-mcp
```

Or add to your MCP client (Claude Desktop, Cursor, Claude Code, Codex, Gemini CLI):

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

Keyless by default — an ephemeral identity runs the x402 free-flow for all 17 free tools.
No wallet or funding needed to start.

## Tools (18 — 17 free)

All data tools are **free**; only `session_create` settles on-chain.

| Tool | Price | What it does |
|------|-------|--------------|
| `url_reader` | Free | Read any URL as clean text |
| `web_search` | Free | Web search |
| `market_snapshot` | Free | Cross-market price/volume snapshot |
| `token_price` | Free | Current token price (USD) |
| `gas_tracker` | Free | Live gas prices |
| `fear_greed_index` | Free | Crypto Fear & Greed index |
| `token_market_data` | Free | Token market data |
| `wallet_balance` | Free | Wallet balance (Stellar / EVM) |
| `whale_activity` | Free | Large-transfer monitoring |
| `defi_tvl` | Free | Protocol TVL (DeFiLlama) |
| `token_security` | Free | Token security / honeypot check |
| `open_interest` | Free | Futures open interest |
| `orderbook_depth` | Free | Order-book depth + slippage |
| `crypto_news` | Free | Crypto news feed |
| `funding_rates` | Free | Perp funding rates |
| `yield_scanner` | Free | DeFi yield opportunities |
| `dune_query` | Free | Run a Dune query |
| `session_create` | $0.01 | Open a metered, budget-capped spending session |
| `route` | Free | Buyer-side routing: cheapest real x402 tool under budget (advise-only) |

## Config

| Env var | Default | Purpose |
|---------|---------|---------|
| `AGENTPAY_GATEWAY_URL` | `https://agentpay.tools` | Point at a different gateway |

## Pay for tools (Python SDK)

The Node server is keyless and runs the free tools. To settle paid tools and get hard
budget caps + receipts, use the Python SDK:

```bash
pip install agentpay-x402
```

```python
from agentpay import quickstart
s = quickstart(max_spend=0.10)              # one hard cap, no funding to start
print(s.call("token_price", {"symbol": "ETH"}).data["price_usd"])
print(s.spending_summary())                 # receipt: every call, cost, tx, chain
```

## Privacy Policy

AgentPay is built for autonomous agents and does not collect names, emails, or other personal
identifiers. The MCP server can run keyless (ephemeral identity). It processes tool-call metadata
(wallet address, tool name, parameters, amount, tx hash, timestamp) to operate the service and
forwards requests to upstream public data providers. Full policy: **https://agentpay.tools/privacy**

GitHub: https://github.com/romudille-bit/agentpay
