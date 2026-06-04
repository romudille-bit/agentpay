# AgentPay plugin for Claude Code

**Buyer-side economic intelligence for your agent.** When the agent needs a paid tool, AgentPay
helps it *find, judge, and pay for the best x402 service within a budget* — discovering across
the marketplace, dropping stubs, ranking by **real usage (not price)**, and capping + receipting
the spend. Peer-to-peer (AgentPay never holds funds), multi-chain (USDC on Base or Stellar).

## Install
```
/plugin marketplace add romudille-bit/agentpay
/plugin install agentpay@agentpay
```
Then `/reload-plugins`. No keys or accounts needed to route.

## What you get
- **17 free crypto/data tools via MCP** — token prices, gas, DeFi TVL, whale activity, news,
  yield scanner, and more. Zero setup: no wallet, no keys, no USDC. Wired automatically via
  `.mcp.json` (`npx @romudille/agentpay-mcp`, pure Node, no Python).
- **`agentpay-route` skill + bundled router** (`agentpay-route "<need>" --budget <usdc>`):
  queries Coinbase Bazaar, drops keyword-stuffed/empty/factory stubs, ranks survivors by real
  usage (unique payers, calls, recency), enforces the budget, and recommends the best provider —
  "cheapest that's real and used," never cheapest. Pure stdlib, runs with just `python3`.
- **`agentpay-session` skill**: cap + receipt the spend via the AgentPay SDK once a tool is
  chosen (`pip install agentpay-x402`).

## How it works
1. `agentpay-route "funding rates" --budget 0.01` → ranked candidates + a recommendation.
2. The agent picks (taste) and pays the provider directly via the SDK session (cap + receipt).
3. AgentPay advises and governs spend; it never custodies funds.

## Roadmap
- `route` MCP tool (discover + rank + return ready-to-pay details, no wallet).
- Delivery-quality signal from AgentPay's own routing telemetry (Bazaar gives usage; we add
  "did it actually deliver").

Home: https://agentpay.tools · SDK: `pip install agentpay-x402`
