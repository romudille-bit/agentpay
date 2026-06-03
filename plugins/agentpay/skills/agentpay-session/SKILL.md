---
name: agentpay-session
description: >
  Cap and receipt an AI agent's spending. Open a budget-capped AgentPay session so paid
  tool/API calls stay under a hard USDC limit and each produces a verifiable receipt + a
  running ledger. Use when enforcing a spend cap across calls, tracking what an agent spent,
  or producing an audit trail for autonomous payments. Pairs with the agentpay-route skill.
---

# AgentPay — budget-capped session + receipts

Once the agent has chosen a paid tool (see the **agentpay-route** skill), wrap the payment in
an AgentPay session so spend stays under a hard cap and every call leaves a verifiable receipt.
This is the spend-governance layer: budget enforcement + receipts + a running ledger across
calls, multi-chain (USDC on Base or Stellar), peer-to-peer (AgentPay never holds funds).

## When to use
- Enforce a hard USDC cap across an agent's paid calls.
- Track / audit what the agent spent (per call: cost, tx, chain).
- Produce a receipt or ledger for autonomous payments.

## How
```
pip install agentpay-x402
```
```python
from agentpay import Session, AgentWallet
s = Session(AgentWallet(secret_key="S...", base_key="0x..."), max_spend="0.05")

s.remaining_usd()                  # budget left, before committing to a call
r = s.call("<provider-url-or-tool>", {...})   # pays within the cap; raises if over budget
print(s.spending_summary())        # receipt: each call's cost + tx + chain + remaining
```
Zero-setup variant for the free tools / trying it: `from agentpay import quickstart; s = quickstart(max_spend="0.10")`.

## Principles (honor these)
- The hard cap is **SDK/session-enforced (client-side)**; the gateway issues the session id,
  receipts, and ledger. Don't claim wallet-level or server-side enforcement.
- **Base is the default paid chain**, Stellar the automatic fallback (`prefer_chain="stellar"`
  to override). Free ($0) tools never settle on-chain and need no wallet.
- Peer-to-peer: the agent pays the provider directly; AgentPay caps + receipts, never custodies.

Home: https://agentpay.tools · SDK: pip install agentpay-x402
