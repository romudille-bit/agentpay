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

**New in v2.8.0 — pay in sBTC on Stacks, and every paid result carries its receipt.** Set
`AGENTPAY_STACKS_KEY` (a Stacks private key, 64 or 66 hex) and paid tools settle in **sBTC on
Stacks** — sponsored (no STX for fees) whenever the gateway's 402 offers it, so the wallet
needs only sats. The MCP signs a complete transaction with an exact-amount post-condition and
never broadcasts; the gateway (or its relay) does. Every paid result, on either chain, now
comes back with a `receipt` (tx, network, amount, session spend vs cap, explorer link), and the
new `session_cap` tool shows the cap and lets the user lower it before the first paid call.

To rotate the wallet, stop the MCP and delete (or move) `~/.agentpay/mcp-wallet.json` — a
fresh key is minted next start. Sweep any USDC out first; the key never leaves the file.

Gateway: `https://agentpay.tools`

## Remote MCP (claude.ai, ChatGPT, Perplexity, Grok)

No install: add `https://agentpay.tools/mcp` as a custom connector (Streamable HTTP, no auth). It
serves the keyless set — 17 free tools, `verified_route_preview` and `estimate_plan`. The
$0.01 tools (`pre_trade_check`, `funding_spread`, `sbtc_peg`, `verified_route`, `session_create`) answer an unpaid call with
the x402 PaymentRequired, which a wallet-carrying MCP client pays in-band; without one, use the
local server with a wallet (below) or the SDK. Per-harness steps:
[agentpay.tools/connect](https://agentpay.tools/connect).

---

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
| `pre_trade_check` | $0.01 | Composite pre-trade verdict: orderbook + funding + OI + security (Bitflow venue for Stacks tokens) |
| `funding_spread` | $0.01 | Cross-venue perp funding spread: nine venues normalised to 8h, cheapest/dearest, 7-day percentile, carry at your size |
| `sbtc_peg` | $0.01 | The sBTC peg: supply vs cap, 24h/7d peg-in/out flows, in-flight bridge requests, signer set, withdrawal headroom |
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
| `AGENTPAY_MAX_SPEND` | `0.10` | Hard session spend cap in USD (paid mode) — calls past the cap are refused; `session_cap` can lower it per session |
| `AGENTPAY_STACKS_KEY` | *(unset)* | Stacks private key (64/66 hex, never persisted): paid tools settle in sBTC on Stacks; implies paid mode |
| `AGENTPAY_STACKS_NETWORK` | `mainnet` | `mainnet` or `testnet` for the Stacks key |
| `AGENTPAY_STACKS_SPONSORED` | `auto` | `auto` signs a sponsored (fee-0) tx when the 402 offers it; `1` requires it; `0` pays its own STX fee |
| `AGENTPAY_PREFER_CHAIN` | `stacks` with a Stacks key, else `base` | Which rail to settle on when a 402 offers both |

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

### Stacks (sBTC)

```json
{
  "mcpServers": {
    "agentpay": {
      "command": "npx",
      "args": ["-y", "@romudille/agentpay-mcp"],
      "env": {
        "AGENTPAY_STACKS_KEY": "<64- or 66-hex Stacks private key>",
        "AGENTPAY_MAX_SPEND": "0.10"
      }
    }
  }
}
```

Fund the key's address (printed on stderr at startup) with a few sats of sBTC. When the
gateway advertises `sponsorship` in its 402 the MCP signs a sponsored transaction with fee 0
and the gateway's relay pays the STX fee; otherwise keep a little STX in the wallet for fees.
The transaction carries a deny-mode "exactly N sats leave the sender" post-condition, so the
gateway can settle at most the quoted amount. An unconfirmed settle is redeemed on the next
call of the same tool — the MCP never signs a second payment for it.

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

## Recipe — pre-trade guardrail alongside Coinbase for Agents

Coinbase for Agents gives an agent trading (`orders_preview` / `orders_create`) and a curated
x402 data catalog. AgentPay adds the step that catalog doesn't have: a one-call risk verdict
*before* the order. Run the two MCPs side by side — AgentPay is not part of Coinbase's catalog
and settles its own $0.01 from the small wallet you give it (paid mode above).

Prompt to paste into the session:

> Trading guardrail: before every Coinbase orders_create, call AgentPay pre_trade_check with {"symbol": <asset>, "side": <long|short>, "size_usd": <notional>}. Proceed only if the verdict is "ok". On "caution" or "avoid", show me the per-factor reasons and ask before doing anything else. For data the Coinbase catalog does not cover, call AgentPay verified_route first and use the provider it picks. Never exceed AGENTPAY_MAX_SPEND.

The verdict comes back as `ok` / `caution` / `avoid` with a per-factor breakdown (slippage at
your size, funding carry, OI crowding, contract security) and the raw components embedded, so
the agent can show its reasons instead of just refusing.

## Privacy Policy

AgentPay is built for autonomous agents and does not collect names, emails, or other personal
identifiers. The MCP mints a local wallet identity on first run (stored only on your machine;
the private key is never sent anywhere — payment signatures are computed locally). It processes tool-call metadata
(wallet address, tool name, parameters, amount, tx hash, timestamp) to operate the service and
forwards requests to upstream public data providers. Full policy: **https://agentpay.tools/privacy**

GitHub: https://github.com/romudille-bit/agentpay
