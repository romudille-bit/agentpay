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
seller has delivered before, and refuses one that has taken payment without delivering. The
cap is checked on the quote that is about to be signed, in USDC only: a seller that raises
its price between the check and the payment, or asks for another token, is refused.

## What we found

We hired the cheapest available agents on Agent Market with real USDC, one at a time:

| | |
|---|---|
| Hires that ran their course | 25 |
| **Returned nothing** | **11 — 44%** |
| Delivered | 14 |
| Spent waiting for results that never came | 8.5 hours |
| Delivered work received | 9.9 minutes |
| Charged | $0.50 of $1.05 quoted — only the delivered ones |

We signed a payment for 28 agents. Three of those we cancelled ourselves before the agent's
listed deadline, so they are shown on the page and left out of the count. One more agent
could not be reached at all: no price, nothing signed, not counted.

Published with every row: **[the results page](https://romudille-bit.github.io/agentpay/near/)**.

The marketplace's escrow is exact — nothing that failed to deliver was charged. We checked
that on chain: the payer's USDC balance on NEAR fell by exactly $0.81 over the run, which is
$0.50 for the fourteen delivered hires, $0.01 for the direct purchase and $0.30 sent to Base
through NEAR Intents. The cost is time instead: an agent that goes quiet burns its whole
payment window before a buyer can be sure.

The catalog does not separate the two. The agents with the longest delivered-job histories
are among the ones that never answered: `codecitationdrift` advertises 28 jobs at 90%,
`web-assets` 15 at 75%, `luigi-check` 5 at 100% — all three took the hire and returned
nothing, and `luigi-check` still reads 100% today. Thirteen of the fourteen that did deliver
had one to three jobs to their name.

Paying and watching is the signal that holds. That is what this reads before it spends, and
what AgentPay's `verified_route` sells on other chains. The marketplace already has what it
needs to show the same thing in the catalog: delivery measured over paid hires, and the date
an agent last delivered.

Funding a payment on another chain from NEAR is the slow part: of three attempts through
NEAR Intents, two timed out and refunded, and the one that worked took over two hours to
land 0.2975 USDC on Base. The quote for that route estimates 24 seconds. On a one-cent
purchase the swap also adds 26–50% to the price, because of the minimum swap size.

How to read the numbers:

- **Delivered is generous.** A completed task with any non-empty result counts; we do not
  grade the answer. The share that returned nothing is a floor.
- **Targets.** `targets.json` holds 37 agents picked from the catalog on 12 Nov: listed as
  available, priced at $0.10 or less, each with a brief written for what it advertises. The
  sweep got through 28 of them; one more was hired by hand before the sweep existed.
- **Void rows.** Eight earlier rows in `docs/hires.json` are void and kept in the log: our
  error, payments signed in a burst from one key.
- **Our cancel rule.** A task that had not started was cancelled; a cancelled task moves no
  money. The first sweep did this after two minutes whatever deadline the agent listed, which
  is why three hires are out of the count. The code now waits the listed deadline.
- **Our payment window.** The marketplace rejects a payment signed for the full quoted
  window, so we sign for 80% of it, and that lifetime is counted in blocks, which are faster
  than a second. Our authorization therefore lapses before the quoted window does. Every
  delivery arrived within two minutes, far inside it, but an agent that answered very late
  could have been turned away for that reason.

## Do X, see Y

Needs Node 22 or later. These three cost nothing — no key, no wallet, no payment.

```bash
cd near && npm install
npm test
#   → 42 tests pass

npm run check -- https://geoip.bip-rep.com/lookup
#   → "verdict": "block" — a seller our probes paid that returned nothing. Exit code 2.

npm run check -- https://www.x402financialdata.com/crypto-prices
#   → "verdict": "pass", "rail": "base-via-intents", the seller's live price, and what
#     funding it costs in NEAR
```

Paying needs a NEAR account holding a little USDC on NEAR. Put `NEAR_ACCOUNT_ID` and
`NEAR_PRIVATE_KEY` in `near/.env` (a `.env` in the repository root is read too; `near/.env`
wins). The key must be full-access: `ft_transfer` needs a 1 yoctoNEAR deposit, which a
function-call key cannot attach. Then:

```bash
npm run hire -- harvest --task "One question
What is NEP-366, in two sentences?" --max 0.10
#   → quotes the agent, pays, waits, and prints whether it delivered. Over the cap, or
#     quoted in anything but USDC: refused before signing.

npm run buy -- "https://quittance-five.vercel.app/api/receipt?q=<a swap deposit address>"
#   → pays USDC on NEAR and returns the settlement transaction with the data

npm run sweep -- --budget-s 3600
#   → works through targets.json one hire at a time, writing docs/hires.json

npm run funding && npm run page
#   → re-reads the funding attempts and rebuilds docs/index.html from the log
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
