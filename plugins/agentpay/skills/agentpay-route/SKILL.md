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

**Option A — MCP `route` tool (any MCP-capable agent, no setup):**

If your agent runtime has the AgentPay MCP connected (`npx @romudille/agentpay-mcp`), call
the `route` tool directly — no Python, no CLI, no repo:
```json
{ "tool": "route", "arguments": { "need": "funding rates", "budget": 0.01 } }
```
Returns a ranked candidate list + recommendation + `ready_to_pay` details (provider URL +
x402 `accepts` entry) as JSON. Advise-only, keyless, works in any MCP runtime.

**Option B — bundled CLI router (Claude Code / shell, pure stdlib, no setup):**
```
agentpay-route "<what you need>" --budget <max USDC, e.g. 0.01>
```
It queries Coinbase Bazaar, drops stubs (no schema, keyword-stuffed, or factory clones —
one wallet behind many fake names), ranks survivors by **real usage** (unique payers > raw
calls, plus recency), enforces the budget, and prints a ranked table + a recommendation with
the provider URL and *why*.

**2. Apply taste.** The router supplies price / quality / legitimacy; the agent makes the
final capability call — does this tool actually return the field the task needs? Pick from the
ranked list (usually the ★ recommendation).

**3. Pay the provider directly, capped + receipted (peer-to-peer).** Use the AgentPay SDK so
the spend stays under a hard cap and every call produces a verifiable receipt + ledger:
```
pip install agentpay-x402
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
- **Peer-to-peer, no custody.** AgentPay advises and (via the SDK session) caps + receipts the
  spend; the agent pays the provider directly. AgentPay never holds funds.

Home: https://agentpay.tools · routing is advise-by-default (you choose and pay).
