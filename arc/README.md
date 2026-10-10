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

## Findings — run of 10 Oct 2026

Twelve paid calls across all eight providers, both settlement domains, $0.30 of USDC. Three full runs
on the same day agreed with each other.

- **Every provider answered 402 before touching the request** (12/12), and every live challenge matched
  the catalog price. Only one listed-vs-live gap, and it is in the schema, not the price: LoneStarOracle's
  earnings calendar marks `tickers` optional, the live API rejects with 400 without it — before any
  payment, and it says so in the body.
- **Charged, not delivered: AIsa.** A call missing the `date` parameter was settled for $0.10 through
  Circle Gateway (transfer visible in the Gateway ledger) and then rejected with 400 "missing argument
  'date'". AIsa's own OpenAPI spec says 4xx/5xx are not charged; on the x402 path they are. The same
  listing with the right parameters delivers in ~1.8 s.
- **Delivered, no receipt.** Three deliveries came back without a `PAYMENT-RESPONSE`: BlockRun's nano
  (Gateway) and arc (EIP-3009) endpoints, and Goldsky. The probe found BlockRun's two charges
  independently — one in Arc's transfer logs, one in the Gateway ledger — so the buyer was charged and
  simply not told. Goldsky is different: its $0.005 authorization is a prepaid balance "for ~1000
  requests"; the first run was charged once, later runs were served with no charge at all. A buyer holding
  only the HTTP response cannot tell these two cases apart.
- **Validation after signature, before charge: Orthogonal.** The findymail proxy returned 400 "Missing
  required parameters" after the payment was signed, and no settlement exists for it. Correct behaviour,
  worth recording because it is the opposite of the AIsa case.
- **Sign-in-with-wallet is not enforced for x402.** Quicknode's listings carry `siwx: true`; a plain
  Gateway payment was served with a receipt.
- **Latency by settlement domain.** Gateway-batched calls returned in 0.9–1.8 s; EIP-3009 calls took
  4.0–4.7 s, which is the facilitator's on-chain settlement before the response. Unpaid 402s took
  0.1–1.9 s.
- **Proof.** Five calls left an Arc transaction, five a Gateway transfer id, two nothing. Twelve
  attestations on `ReceiptAnchor`, including the failures, so the ledger can be checked against the
  chain later.

Things the probe had to learn: a fresh Gateway deposit is not spendable for ~10–20 s (Exa correctly
rejected the first attempt with `insufficient_balance`); the Gateway transfer ledger lags the response
by a few seconds; and the catalog itself moved during the day (2,144 → 2,195 listings, EIP-3009 options
90 → 141).

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
