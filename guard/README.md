# @romudille/x402-guard

A delivery guard for the official x402 client. Before your agent signs a payment, the guard looks the resource up in [AgentPay's paid-probe scores](https://agentpay.tools/scores.json) and aborts, warns, or passes. Resources we have no data on always pass.

It answers two questions the x402 client does not: *has this endpoint taken money and not delivered?* and *how much has this run spent in total?* The client's own `spendControls` cap each payment; this one caps who you pay and what the whole session adds up to — and keeps the receipt.

## Install

```bash
npm install @romudille/x402-guard
```

Node ≥ 18, no dependencies.

## Use with `@x402/core`

```js
import { x402Client } from '@x402/core/client';
import { wrapFetchWithPayment } from '@x402/fetch';
import { deliveryGuard } from '@romudille/x402-guard';

const client = new x402Client()
  .register('eip155:8453', yourEvmScheme)
  .onBeforePaymentCreation(deliveryGuard());

const fetchWithPay = wrapFetchWithPayment(fetch, client);
const res = await fetchWithPay('https://some-x402-service.example/v1/thing');
```

That is the whole integration. The hook runs on every 402 the client is about to pay. On a `block` verdict it returns `{ abort: true, reason }`, the client throws `Payment creation aborted: x402-guard: …`, and nothing is signed.

### Options

```js
deliveryGuard({
  mode: 'abort',          // 'abort' (default): abort on block, log on warn
                          // 'warn':   never abort on a verdict, only log
                          // 'strict': abort on block and on warn
  sessionBudget: { maxTotalUsd: 0.50 },   // optional cumulative cap for this process
  amountUsd: (req) => Number(req.amount) / 1e6,   // default: 6-decimal stablecoin atomic units
  ttlMs: 600_000,         // how long one scores.json fetch is reused
  thresholds: { block: 0.5, pass: 0.9 },
  onDecision: (d) => {},  // every lookup — see "The ledger"
  logger: (msg) => {},    // where warn/block/over_budget lines go (default: console.warn)
});
```

### Session budget and the ledger

With `sessionBudget`, the hook keeps a running total of the payments it let through and aborts the first one that would take the total past `maxTotalUsd` — in every mode, including `warn`. A payment blocked for delivery reasons never counts. The total is per process and in memory: it resets when the agent restarts, and nothing enforces it beyond this hook. Durable caps across runs, with signed receipts, are what an [AgentPay session](https://agentpay.tools) is for.

Every decision is kept:

```js
const guard = deliveryGuard({ sessionBudget: { maxTotalUsd: 0.5 } });
client.onBeforePaymentCreation(guard);
// later
guard.spent();    // 0.03
guard.ledger();   // [{ at, url, verdict, reason, aborted, network, amount, amountUsd, budget: { spent, max }, match, entry, alternative }, …]
```

`verdict` is one of `pass`, `warn`, `block`, `unknown`, `over_budget`.

### When a seller is blocked

Every scored resource carries the need it was probed for. If other scored providers deliver for that need, a blocked or warned decision says so and points at `verified_route`:

```js
d.alternative   // { need: 'llm inference', scored_providers: 3, verified_route: 'https://agentpay.tools/tools/verified_route/call' }
d.reason        // '… — 3 scored providers deliver for "llm inference"; ask verified_route'
```

`verified_route` is AgentPay's paid tool ($0.01) that returns the vetted provider for a need with a ready-to-pay x402 challenge; the guard only tells you it exists.

### One-off check

```js
import { checkUrl } from '@romudille/x402-guard';
const d = await checkUrl('https://some-x402-service.example/v1/thing');
// { url, verdict: 'pass'|'warn'|'block'|'unknown', reason, match: 'exact'|'prefix'|null, entry }
```

CLI, exit code 2 if anything blocks:

```bash
npx @romudille/x402-guard check https://a.example/v1/x https://b.example/v2/y
```

## Verdicts

Each scores.json entry carries the last 30 days of *paid* probes against that resource: how many, how many delivered, when it last delivered and last failed, and a `took_payment_no_delivery` flag.

| Verdict | When |
|---|---|
| `block` | flagged `took_payment_no_delivery` **and** either ≤ 50 % of paid probes delivered or the most recent probe is the failure |
| `warn` | flagged but delivered since; or < 50 % delivery; or < 90 % delivery; or a single probe (`provisional`); or settle failures |
| `pass` | confirmed, ≥ 90 % delivered |
| `unknown` | not in scores.json, or no paid probe in the window |

Thresholds mirror the prober's own scoring and are configurable. `unknown` never blocks: absence of data is not evidence.

URL matching is exact after normalisation (host lowercased, no query, no trailing slash), with a fallback to a scored parent path on the same origin (`match: 'prefix'`).

## What it fetches, and what it does not send

- One `GET https://agentpay.tools/scores.json` per `ttlMs` (default 10 min), cached in memory. No request body, no wallet, no resource URL leaves your process.
- If the fetch fails, the last good copy is kept; with none, every verdict is `unknown`. The guard fails open by design — a scores outage must never stop your agent from paying.
- Nothing is reported back. Decisions, the budget and the ledger stay in your process (`onDecision` and `ledger()` are yours to use).
- The hook is client-side, like the client's own `spendControls`: it protects an agent from bad sellers and runaway runs, not from code that bypasses the hook.

## Coverage

The scores cover the services AgentPay's prober has paid with real USDC on Base and Solana, selected from what the x402 Bazaar surfaces for a set of common needs. Coverage grows with each sweep (Mon/Thu); a resource outside it is `unknown`, not endorsed. Field names and the verdict rules are stable within 0.x; the service set is not.

## Tests

```bash
npm test
```

Fixtures are synthetic. The decision rules and the hook contract are also exercised against the real `@x402/core` client in development.

## Licence

MIT — romudille / [AgentPay](https://agentpay.tools)
