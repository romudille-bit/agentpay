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
the paid `verified_route` ($0.01). (`route` is kept as a legacy alias; `estimate_plan`
prices a multi-tool plan before you spend.)

**New in v2.5.0 — a wallet per install, spending off by default.** On first run the MCP
mints an EVM key and keeps it in `~/.agentpay/mcp-wallet.json` (file mode 0600; override the
location with `AGENTPAY_WALLET_PATH`). That address is this install's stable identity —
free calls carry it, and it's the account you'd fund. **Nothing can spend from it until you
say so:** paid tools settle **in-place** (gasless EIP-3009 on Base, no ETH needed, nothing
broadcast client-side — a rejected call moves no USDC) only when you set
`AGENTPAY_ENABLE_PAID=1` after funding the wallet with USDC on Base, or when you bring your
own key via `AGENTPAY_BASE_KEY` (which implies paid mode, as in v2.4.x). Spending is always
capped by `AGENTPAY_MAX_SPEND`. In paid mode, `verified_route` returns the **full paid
payload** — provider URL + ready-to-pay challenge; otherwise it's the free preview.

To rotate the wallet, stop the MCP and delete (or move) `~/.agentpay/mcp-wallet.json` — a
fresh key is minted next start. Sweep any USDC out first; the key never leaves the file.

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

## Tools (17 free + paid)

All data tools are **free**. Paid tools ($0.01) need wallet mode (`AGENTPAY_BASE_KEY`) —
or the `agentpay-x402` Python SDK.

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
| `pre_trade_check` | $0.01 | Composite pre-trade verdict: orderbook + funding + OI + security |
| `verified_route` | Free preview / $0.01 full | Buyer-side trust oracle: the vetted, real x402 provider for a need (full payload in wallet mode) |
| `route` | Free | Legacy alias of the `verified_route` preview |
| `estimate_plan` | Free | Price a multi-tool plan before spending |

## Config

| Env var | Default | Purpose |
|---------|---------|---------|
| `AGENTPAY_GATEWAY_URL` | `https://agentpay.tools` | Point at a different gateway |
| `AGENTPAY_ENABLE_PAID` | *(unset — off)* | `1` to settle paid tools from this install's persisted wallet (fund it first) |
| `AGENTPAY_BASE_KEY` | *(unset)* | Bring-your-own EVM private key; implies paid mode (v2.4.x behaviour) |
| `AGENTPAY_WALLET_PATH` | `~/.agentpay/mcp-wallet.json` | Where the minted wallet lives (sandboxed hosts: point somewhere writable) |
| `AGENTPAY_MAX_SPEND` | `0.10` | Hard session spend cap in USDC (paid mode) — calls past the cap are refused |

## Paid mode (settle paid tools in-place)

The startup line on stderr prints this install's wallet address. Fund it with USDC on
Base mainnet, then:

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

Or bring your own key:

```json
{
  "mcpServers": {
    "agentpay": {
      "command": "npx",
      "args": ["-y", "@romudille/agentpay-mcp"],
      "env": {
        "AGENTPAY_BASE_KEY": "0x<your EVM private key>",
        "AGENTPAY_MAX_SPEND": "0.10"
      }
    }
  }
}
```

Fund the key's address with USDC on Base mainnet — that's it. No ETH needed: settlement is
gasless EIP-3009 (`transferWithAuthorization`); the MCP signs off-chain and the gateway's
facilitator settles only if it accepts the call, so a rejected call moves no USDC. Every
paid call counts against `AGENTPAY_MAX_SPEND`; the MCP refuses calls that would exceed it.

Use a dedicated, small-balance key for agent spend — the cap is your blast radius.

Prefer Python, or need Stellar settlement and full receipts? The `agentpay-x402` SDK:

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
identifiers. The MCP mints a local wallet identity on first run (stored only on your machine;
the private key is never sent anywhere — payment signatures are computed locally). It processes tool-call metadata
(wallet address, tool name, parameters, amount, tx hash, timestamp) to operate the service and
forwards requests to upstream public data providers. Full policy: **https://agentpay.tools/privacy**

GitHub: https://github.com/romudille-bit/agentpay
