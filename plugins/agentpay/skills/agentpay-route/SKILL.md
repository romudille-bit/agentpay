---
name: agentpay-route
description: >
  Find, judge, and pay for the best paid x402 tool/API for a need, within a budget.
  Use when the agent needs a capability behind a paid API and must choose a provider:
  "which x402 service / paid API for X", "find a tool for X under $Y", comparing
  providers, avoiding overpriced or fake/stub endpoints, or routing agent spend wisely.
  Picks "the cheapest tool that's real and actually used" — never cheapest, never a stub.
---

# AgentPay — route to the best x402 tool, within a budget

When the agent needs a paid capability it doesn't already have, use AgentPay to **discover
candidate x402 tools across the marketplace, judge which one is real and actually used (not
the cheapest, not a keyword-stuffed stub), pay the best one under a budget, and keep a
receipt.** This is buyer-side economic intelligence: the agent shops the marketplace instead
of grabbing the first or cheapest result.

## When to use
- The agent needs data/capability behind a *paid* API and must pick a provider.
- "which x402 tool for X", "find a paid API for X under $0.01", "compare these providers".
- You want to avoid overpaying or paying a fake/empty endpoint.

## How

**1. Vet the marketplace — the AgentPay MCP (any MCP-capable runtime, no Python, no repo).**

If the AgentPay MCP isn't connected yet, wire it first (Node >= 18):

- Claude Code: `claude mcp add agentpay -- npx -y @romudille/agentpay-mcp`
- Any other runtime, in its MCP config:

```json
{ "mcpServers": { "agentpay": { "command": "npx", "args": ["-y", "@romudille/agentpay-mcp"] } } }
```

Then call **`verified_route`** — the buyer-side trust oracle. It sweeps the whole x402
catalog across many queries, collapses sybil/factory clusters (one wallet stamping many
"distinct" tools → one entry), ranks the genuinely-used survivors, and recommends within
budget:

```json
{ "tool": "verified_route", "arguments": { "need": "dex pair liquidity", "budget_usd": 0.01 } }
```

- **Keyless (default): free preview** — the vetted pick's name, usage stats, survivor count,
  and *why*. Proof of vetting, without the actionable payload.
- **Wallet mode: full paid payload ($0.01)** — set `AGENTPAY_BASE_KEY` (dedicated
  small-balance EVM key, USDC on Base) and `AGENTPAY_MAX_SPEND` (session cap, default $0.10)
  in the server's `env`. The MCP settles the call in-place (gasless EIP-3009, refused
  BEFORE signing once the cap is hit) and returns the provider URL + ready-to-pay x402
  challenge.

Also available: `route` (legacy single-query ranking, keyless) and `estimate_plan` (price a
multi-tool plan before spending, free).

**Bundled CLI alternative (Claude Code *plugin* installs only):** if this skill arrived via
`/plugin install agentpay@agentpay`, the pure-stdlib router is on PATH —
`agentpay-route "<need>" --budget 0.01` prints the same ranked table + recommendation.
Installed via `npx skills add`? Use the MCP above instead; the CLI is not on PATH.

**2. Apply taste.** The router supplies price / quality / legitimacy; the agent makes the
final capability call — does this tool actually return the field the task needs? Pick from the
ranked list (usually the recommendation). A pick flagged `probe_coverage` has never had its
delivery verified by a paid probe on that network — weigh accordingly.

**3. Pay the provider directly, capped + receipted (peer-to-peer).** In MCP wallet mode the
settle already happened in-place under `AGENTPAY_MAX_SPEND`. From Python, use the AgentPay
SDK so the spend stays under a hard cap and every call produces a verifiable receipt + ledger:
```
pip install "agentpay-x402[base]"
```
```python
from agentpay import Session, AgentWallet
s = Session(AgentWallet(secret_key="S...", base_key="0x..."), max_spend="0.05")
r = s.call("<chosen-provider-url>", {...})   # pays the provider directly via x402
print(s.spending_summary())                   # receipt + running ledger
```

## Principles (honor these)
- **Never pick on price alone.** A $0.005 endpoint returning `{}` is worse than a $0.001 tool
  with 25 real payers. The router encodes this — trust its ranking over raw price.
- **Respect the budget.** If nothing real fits the cap, the router says so — don't pay for a
  stub to "use the budget."
- **Peer-to-peer, no custody.** AgentPay advises and (via the MCP cap or SDK session) caps +
  receipts the spend; the agent pays the provider directly. AgentPay never holds funds.

Home: https://agentpay.tools · routing is advise-by-default (you choose and pay).
