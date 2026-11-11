# near-check

A pre-payment delivery check and paid fetch for agents that hold funds on NEAR.

Before an agent pays an x402 seller, `near-check` answers three questions:

1. **Does this seller deliver?** It looks the URL up in AgentPay's public paid-probe scores
   (real payments to 450+ x402 endpoints, checked for whether the data came back).
   A seller flagged for taking payment without delivering is blocked.
2. **How would a NEAR account pay it?** It reads the seller's live 402. If the seller takes
   USDC on NEAR, the agent pays directly. If it only takes USDC on Base, the payment is funded
   from NEAR through NEAR Intents (1Click).
3. **What does that cost?** A live 1Click quote: NEAR in, exact USDC out on Base, with the
   overhead and the time estimate.

Status: the check and the quote work today. The paying legs and receipts are in progress.

## Try it

Needs Node 22 or later. No key, no wallet, nothing is paid.

```bash
cd near && npm install
npm test                                                     # 12 tests pass
npm run check -- https://geoip.bip-rep.com/lookup            # see "verdict": "block", exit code 2
npm run check -- https://agentpay.tools/tools/verified_route/call   # see the 402 price and the NEAR cost
npm run quote -- 1                                           # see what 1 USDC on Base costs in NEAR
```

## NEAR's role

- **NEAR Intents (1Click)** funds payments to sellers on other chains from a NEAR account.
  There is no Intents testnet, so quotes and swaps are mainnet.
- **x402 on NEAR** (`exact` scheme, USDC, gas paid by the facilitator) is the direct rail
  when the seller supports it.

## Disclosure

Built for NEAR HACKATHON 2026. Tracks: AI Security & Defense, Agent Economy.

- Everything under `near/` is hackathon work. The diff is `pre-hackathon...hackathon-submission`.
- The rest of this repository is AgentPay, an existing product on Base, Stellar and Stacks.
  It is baseline and not part of the entry.
- Libraries: `@romudille/x402-guard` (ours, published before this entry; reads the public
  scores at agentpay.tools/scores.json). Code was written with AI assistance.

MIT, see the repository `LICENSE`.
