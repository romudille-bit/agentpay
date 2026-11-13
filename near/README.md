# near-check

**An agent with a NEAR account, buying things — and checking whether it got them.**

A buyer on NEAR can pay for work three ways. `near-check` does all three, caps every
payment, and records what actually came back:

1. **Hire an agent** on [NEAR Agent Market](https://market.near.ai) — 691 listed agents,
   every one payable per call in USDC on NEAR over A2A + x402.
2. **Pay an x402 endpoint** that takes USDC on NEAR directly.
3. **Pay an x402 endpoint on another chain**, funded from NEAR through
   [NEAR Intents](https://docs.near-intents.org) (1Click).

Before paying a known endpoint it asks AgentPay's public paid-probe scores whether that
seller has delivered before, and refuses one that has taken payment without delivering.

## What we found

We hired the cheapest available agents on Agent Market with real USDC, one at a time:

| | |
|---|---|
| Agents hired | 20 |
| Delivered | 8 |
| Never started, or never answered | 9 |
| Charged | $0.40 — only the delivered ones |

Published with every row: **[the results page](https://romudille-bit.github.io/agentpay/near/)**.

Two things stand out. The marketplace's escrow is honest: nothing that failed to deliver
was charged. But the catalog is not a buying signal — agents listed as *available now*,
one of them at a 100% success rate, took the job and never answered. The only way to know
is to pay and watch, which is what this does.

## Do X, see Y

Needs Node 22 or later. These three cost nothing — no key, no wallet, no payment.

```bash
cd near && npm install
npm test
#   → 33 tests pass

npm run check -- https://geoip.bip-rep.com/lookup
#   → "verdict": "block" — a seller our probes paid that returned nothing. Exit code 2.

npm run check -- https://agentpay.tools/tools/verified_route/call
#   → "rail": "base-via-intents", the seller's live price, and what funding it costs in NEAR
```

Paying needs a NEAR account holding a little USDC on NEAR. Put `NEAR_ACCOUNT_ID` and
`NEAR_PRIVATE_KEY` (a full-access key) in `near/.env`, then:

```bash
npm run hire -- harvest --task "One question
What is NEP-366, in two sentences?" --max 0.10
#   → quotes the agent, pays, waits, and prints whether it delivered. Over the cap: refused.

npm run buy -- "https://quittance-five.vercel.app/api/receipt?q=<a swap deposit address>"
#   → pays USDC on NEAR and returns the settlement transaction with the data

npm run sweep -- --budget-s 3600
#   → works through targets.json one hire at a time, writing docs/hires.json

npm run page
#   → rebuilds docs/index.html from the log
```

Every run writes a receipt to `receipts/`: what was quoted, what was paid, what came back,
and the settlement transaction. Response bodies are never stored, only their size and hash.

## NEAR's role

- **x402 on NEAR** is the payment rail. The buyer signs one NEP-366 delegate action
  carrying a NEP-141 `ft_transfer`; a facilitator relays it and pays the gas, so the agent
  needs no NEAR for fees. Both the marketplace and plain x402 sellers settle this way.
- **NEAR Agent Market** is the catalog and the escrow: it holds the payment and releases it
  only when the hired agent delivers.
- **NEAR Intents (1Click)** turns a NEAR balance into USDC on another chain, so one account
  can buy from sellers that have never heard of NEAR. There is no Intents testnet, so this
  path is mainnet with small amounts.

Two things we learned the hard way, both in the code now: the marketplace refuses a payment
signed for the full quoted window (sign for less), and payments signed in a burst from one
key cannot all settle, so hires run one at a time.

## Disclosure

Built for **NEAR HACKATHON 2026**. Tracks: Agent Economy, AI Security & Defense.

- Everything under `near/` is hackathon work, written inside the window. The diff is
  [`pre-hackathon...hackathon-submission`](https://github.com/romudille-bit/agentpay/compare/pre-hackathon...hackathon-submission).
- The rest of this repository is **AgentPay**, an existing product on Base, Stellar and
  Stacks. It is baseline, not part of this entry. The delivery scores this reads come from
  its probe fleet and were collected before the hackathon.
- Libraries: `@x402/near`, `@x402/core`, `@x402/evm`, `@x402/fetch`, `@fastnear/x402`,
  `near-api-js`, `viem`, and `@romudille/x402-guard` (ours, published before this entry).
- Code was written with AI assistance.

MIT — see the repository `LICENSE`.
