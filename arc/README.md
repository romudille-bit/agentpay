# Arc receipt check

Pays live x402 listings on Arc mainnet and publishes what the buyer actually got: delivered or not,
price charged vs listed, latency, and the proof the buyer holds afterwards. Every paid call is
anchored on Arc through a small contract, because Gateway-batched payments leave no transaction of
their own.

Live ledger: https://romudille-bit.github.io/agentpay/ (published from `arc/docs` by `.github/workflows/arc-pages.yml`) · Contract: `ARC_ANCHOR_ADDRESS` in the ledger header.

## Why

Circle's Agent Marketplace lists ~2,100 x402 resources on Arc from eight providers. 96% of them settle
through Circle Gateway: the buyer signs an off-chain authorization, the provider serves the request,
and Circle nets the batch on-chain later. That is what makes sub-cent payments work — and it also
means the buyer walks away with no per-call transaction. The marketplace health-checks reachability,
not delivery, price, or proof. Nobody had paid the catalog and written down what comes back.

This is the buyer-side question AgentPay answers on Base with `verified_route`; Arc is the first chain
where the proof itself is the interesting part.

## What one run does

1. Pulls the Arc catalog from Circle's discovery API (`docs/catalog.json`, shape only).
2. For each target in `targets.json`, sends the request **unpaid** and records status, latency, and the
   402 challenge (does the provider validate the body before asking for money? what does it charge on Arc?).
3. Pays it — through Circle Gateway (`@circle-fin/x402-batching`) for batched listings, through plain
   EIP-3009 (`@x402/core` + `@x402/evm`) for the rest — under a per-call and per-run USDC cap.
4. Records whether the data came back, whether a `PAYMENT-RESPONSE` receipt came with it, and what the
   buyer can point at afterwards (Arc tx, Gateway transfer id, or nothing).
5. Writes a hash of each receipt to `ReceiptAnchor` on Arc, then publishes `docs/ledger.json`.

Response bodies are never published; only their byte size and SHA-256.

The sample covers every provider in the catalog and both settlement domains. One endpoint (Exa
`/contents`) is paid both ways, so the two proof types can be compared on identical data.

## Run it

```bash
cd arc && npm install
npm run wallet -- new          # prints a fresh key; put it in .env as ARC_PRIVATE_KEY
# fund the address with a few USDC on Arc (CCTP from Base, or any exchange that withdraws to Arc)
npm run wallet                 # balances
npm run compile && npm run deploy   # ReceiptAnchor → copy ARC_ANCHOR_ADDRESS into .env
npm run probe                  # pays, anchors, writes docs/ledger.json
npm run probe:dry              # unpaid pass only, no key needed
```

Gas on Arc is USDC, so the same balance covers deposits, attestations, and payments.
`ARC_MAX_SPEND` (default 1.00) caps a whole run; each target carries its own `max`.

## Findings

Filled in after each run — see the ledger. Things already visible from the unpaid pass on 8 Oct 2026:

- 10 of 11 targets answer 402 before touching the request body. LoneStarOracle's earnings calendar
  validates first: the catalog marks `tickers` optional, the live API returns 400 without it, and says so.
- Goldsky serves its RPC against a *signed* authorization before any settlement and returns no receipt
  — its 402 describes the payment as a prepaid balance good for ~1000 requests.
- Exa verifies funds at payment time (`insufficient_funds` on an empty wallet) and challenges $0 on an empty body, $0.001 on a real one.

## What's next

- Run on a schedule and sweep the whole catalog, not a sample.
- Feed the anchored receipts into AgentPay's `verified_route` as typed delivery evidence for Arc.
- AgentPay's own paid tools accepting USDC on Arc, listed on the Circle Agent Marketplace.
- Gateway-batched buying inside the AgentPay SDK and MCP, under a session cap, with the receipt anchored.

## Layout

```
contracts/ReceiptAnchor.sol   event-only anchor: attest(endpoint, receipt, amount, delivered, proof)
scripts/compile.mjs           solc → build/ReceiptAnchor.json
scripts/deploy.mjs            deploy on Arc (viem)
scripts/wallet.mjs            mint a key / print balances
src/catalog.mjs               discovery pull + summary + target resolution
src/pay.mjs                   two buyers: Gateway batched, plain EIP-3009
src/anchor.mjs                attest() on Arc
src/probe.mjs                 the run
targets.json                  the sample, with per-call caps
docs/                         GitHub Pages: index.html, ledger.json, catalog.json, runs/
```
