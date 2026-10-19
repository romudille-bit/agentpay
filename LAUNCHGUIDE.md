# AgentPay — trust routing + spend caps for x402 agents

## Tagline
Buyer-side trust oracle and hard spend caps for agents paying x402 APIs. 17 free tools, no keys.

## Description
Most agent-payment tools are a wallet: they move money. AgentPay is the layer that decides whether to spend it at all.

AgentPay gives an AI agent economic intelligence while it works, not after the bill arrives. Every session opens with a hard USDC budget cap enforced at the payment layer, before any payment is signed. Before paying for an x402 API, the agent can ask `verified_route` which provider is actually worth paying: it sweeps the x402 marketplace (Coinbase Bazaar and x402scan), collapses sybil and factory clones, ranks the survivors by real paying users and delivery, and returns one vetted recommendation with a ready-to-pay challenge. When the session ends, a receipt lists every call, every cost and every settlement transaction.

Out of the box you get 17 free crypto and market-data tools (prices, gas, funding rates, open interest, order-book depth, DeFi TVL, token security scans, whale transfers, Dune queries, web search, URL reader) with no API keys, no wallet and no account. Three paid tools cost $0.01 each and settle over x402 in USDC on Base: `session_create` (capped session with receipt), `pre_trade_check` (one-call trade verdict) and `verified_route` (the trust oracle). Spending is off by default and is bounded by `AGENTPAY_MAX_SPEND` when you turn it on.

Who it is for: developers running agents that buy data or compute from x402 APIs and want to know what a plan should cost and which provider to trust before money moves; trading and research agents that need a pre-trade risk verdict; anyone who wants a priced, receipted session instead of an open wallet.

Runs locally (`npx @romudille/agentpay-mcp`) or as a remote Streamable HTTP server at https://agentpay.tools/mcp (keyless set: 17 free tools, `verified_route_preview`, `estimate_plan`). A Python SDK (`pip install agentpay-x402`) exposes the same sessions with policy controls (allowed tools, per-tool caps, recipient allowlist, approval threshold) and settles in USDC on Base and Stellar or sBTC on Stacks.

## Setup Requirements
- `AGENTPAY_ENABLE_PAID` (optional): Set to `1` to let the server pay for the three $0.01 tools from the wallet it mints on first run (`~/.agentpay/mcp-wallet.json`; fund it with a few cents of USDC on Base). Default off: the 17 free tools work with nothing set. https://github.com/romudille-bit/agentpay/blob/main/README_MCP.md#paid-tools-wallet-mode
- `AGENTPAY_MAX_SPEND` (optional): Hard per-session spend cap in USD when paid mode is on. Calls that would exceed it are refused before anything is signed. Default `0.10`.
- `AGENTPAY_BASE_KEY` (optional): Bring your own EVM private key instead of the minted wallet. Use a dedicated, small-balance key; the cap is your blast radius. Not needed for free tools.
- `AGENTPAY_WALLET_PATH` (optional): Where the minted wallet file is stored. Default `~/.agentpay/mcp-wallet.json`.
- `AGENTPAY_GATEWAY_URL` (optional): Override the gateway. Default `https://agentpay.tools`.

## Category
Finance

## Features
- Hard per-session spend cap enforced before any payment is signed, not in prompt text an agent can ignore
- `verified_route`: buyer-side trust oracle that vets x402 providers for a need and returns one ready-to-pay recommendation
- Sybil and factory-clone collapse: listings from the same wallet or template are merged before ranking
- Ranking by real paying users, call volume and recency rather than self-reported stats
- `pre_trade_check`: one-call ok / caution / avoid verdict with slippage at your size, funding carry, open-interest crowding and optional token security
- `estimate_plan`: price a whole multi-tool plan before spending anything, with cheaper alternatives
- Session receipt: every call, cost, settlement chain and transaction hash
- Anomaly flags for repeated identical paid calls, a single call taking half the cap, and unconfirmed settlements
- 17 free market-data tools with no API keys: prices, gas, funding, open interest, order-book depth, DeFi TVL, yields, token security, whale activity, wallet balances, crypto news, Dune queries, web search, URL reader
- Gasless x402 settlement on Base (EIP-3009): no ETH needed, a rejected call moves no USDC
- Spending off by default; the minted per-install wallet never holds more than you fund it with
- Remote mode with no install for claude.ai, ChatGPT, Perplexity and other hosted MCP clients
- Python SDK with policy parameters: allowed tools, per-tool caps, recipient allowlist, per-call maximum, approval callback above a threshold
- Multi-chain settlement in the SDK: USDC on Base and Stellar, sBTC on Stacks mainnet
- Listed in the official MCP Registry as `io.github.romudille-bit/agentpay`

## Getting Started
- "What's the current ETH price and 24h change?"
- "Check the Fear & Greed Index and the ETH funding rates across exchanges"
- "Before I go long $25k ETH, run a pre-trade check"
- "I need a token security scan API under $0.01 per call. Which x402 provider should I pay?"
- "Estimate what a plan of token_price, pre_trade_check and session_create would cost"
- Tool: verified_route — Find and vet the best paid x402 provider for a need within a budget; returns one recommendation plus the payment challenge. Use before paying any x402 API you have not used before.
- Tool: pre_trade_check — One-call risk verdict for a trade at your size and side. Use before placing an order.
- Tool: session_create — Open a budget-capped session with a receipt. Use when an agent will make several paid calls.
- Tool: estimate_plan — Price a multi-tool plan before spending. Free.
- Tool: token_price, gas_tracker, funding_rates, open_interest, orderbook_depth, defi_tvl, token_security, whale_activity — Free market data, no keys.

## Tags
x402, agent payments, spend cap, budget, trust oracle, provider vetting, receipts, USDC, Base, Stellar, Stacks, crypto prices, market data, DeFi, trading risk, pre-trade check, funding rates, open interest, token security, sybil detection, MCP, AI agents, agentic commerce, micropayments, gasless

## Documentation URL
https://github.com/romudille-bit/agentpay/blob/main/README_MCP.md

## Health Check URL
https://agentpay.tools/health
