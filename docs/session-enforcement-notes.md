# Server-side session cap

Status: **built** (model A below), dark-launched behind `SESSION_ENFORCEMENT`.
Apply `db/migrations/sessions.sql` before turning it on. The original notes
(2026-06-02, "not built — nothing paid to meter yet") are folded into the
design section; the reasoning that made this worth building is at the end.

## What `session_create` does now

- `POST /v1/session/create` and `/tools/session_create/call` store a row in
  `sessions` for the address that **paid**: `max_spend`, `spent`, `status`,
  `expires_at`. Binding to the verified payer means nobody can cap someone
  else's wallet. On Stacks and Base a `session_create` from an address that
  already holds an active session is refused before anything is charged
  (`session_already_active`, the existing session in the body); on Stellar,
  where the payment precedes the request, the existing session is returned
  (`reused: true`). Paying again never raises the cap. Base addresses are
  stored lowercase, so a differently-cased `from` cannot sidestep a session.
  Two creates from one wallet in the same moment charge once: the gateway
  marks the wallet "creating" before it looks the session up, and the
  second is refused (`session_create_in_flight`, retry in a moment) until
  the first is stored or fails.
- On every priced call the payer is known before money moves — Stacks: the
  signed tx's origin; Base: the EIP-3009 authorization's `from`. If that
  address has a live session, the price is reserved atomically
  (`consume_session_budget`, a Postgres function: one row, `FOR UPDATE`,
  `spent + cost <= max_spend`) **before** settlement. Over the cap → 402
  `session_cap_exceeded`, nothing charged, and the call's 402 challenge is
  still usable once the cap frees.
- No header is required, so clients that cannot add one (AIBTC wallet,
  x402-stacks) are covered. `session_id` on the reply lets the SDK show the
  server's view next to its own.
- A settle that charged nothing (node rejection, failed EIP-3009 settle,
  replay refused, an exception mid-settle) releases the reservation, and so
  does a paid call that ends in `refund_pending`. An **uncertain** Stacks
  settle keeps it: the tx may still confirm and be redeemed; a redemption
  that finds the tx aborted releases it then. A release never reopens an
  exhausted session when the payer has since opened a new one.
- `payment_logs.session_id` links every settled call to its session.
  `GET /v1/session/{id}` returns cap, spent, remaining, status, expiry and
  the receipt list (the same rows /ledger chain-verifies).
- Sessions expire (default 24h, `ttl_seconds` on create, bounded by
  `SESSION_MAX_TTL_S`). A session is `exhausted` once less remains than the
  cheapest priced call (nothing can be bought with it), and an exhausted
  session keeps refusing until it expires or the payer opens a new one;
  while more than that remains, cheaper calls still fit and a second
  `session_create` is refused — the refusal body says which case applies.
  Expiry is swept by the gateway's cleanup loop.

## Rails

| rail | when the gateway learns the payer | cap |
|---|---|---|
| Stacks | before broadcast (gateway is the broadcaster) | **enforced** |
| Base (EIP-3009 via CDP) | before `/settle` | **enforced** |
| Stellar | after the agent already paid on-chain | recorded, never refused |

Stellar pays first and presents a tx hash, so refusing there would move money
for nothing. Its calls are recorded against the session even past the cap
(the session reads `exhausted`), so `spent` always matches the receipts.

## Failure rules

- Store outage during the cap check: on enforced rails the call is refused
  with `session_store_unavailable` and nothing charged — the same fail-closed
  rule the replay store already applies to paid calls. Stellar proceeds.
- A reservation that could not be released is logged at critical; the cap
  then over-counts by that amount until corrected (never under-counts).
- `session_create` itself is never reserved against a session, so an
  exhausted cap never locks a wallet out of opening a new one.
- A fake Base signature naming another wallet as `from` reserves that
  wallet's budget for the length of a failed settle, then releases it;
  rate-limited and bounded, accepted.
- `sessions` has row-level security on with no policy and the functions are
  revoked from `PUBLIC`/`anon`: only the gateway's key touches them.

## Why bound to the payer, not a header

Server-side enforcement only adds value over the SDK's client-side cap when
the budget owner is not the code doing the spending. Standard Stacks and Base
x402 clients never load the SDK, so without this they get paid calls and no
cap at all; with it, the wallet's principal sets a cap once and the gateway
holds it regardless of which agent runtime drives the wallet.

## Not built

Model B — prepaid escrow (one payment, many calls, gateway debits per call).
It makes the gateway custodial unless done as an on-chain escrow + atomic
split contract, which stays on the v2 roadmap.
