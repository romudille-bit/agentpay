# Server-side session cap

Status: **built** (model A below), dark-launched behind `SESSION_ENFORCEMENT`.
Apply `db/migrations/sessions.sql` before turning it on. The original notes
(2026-06-02, "not built — nothing paid to meter yet") are folded into the
design section; the reasoning that made this worth building is at the end.

## What `session_create` does now

- `POST /v1/session/create` and `/tools/session_create/call` store a row in
  `sessions` for the address that **paid**: `max_spend`, `spent`, `status`,
  `expires_at`. Binding to the verified payer means nobody can cap someone
  else's wallet. An address that already holds an active session gets that
  session back (`reused: true`); paying again never raises the cap.
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
  replay refused) releases the reservation. An **uncertain** Stacks settle
  keeps it: the tx may still confirm and be redeemed; a redemption that finds
  the tx aborted releases it then.
- `payment_logs.session_id` links every settled call to its session.
  `GET /v1/session/{id}` returns cap, spent, remaining, status, expiry and
  the receipt list (the same rows /ledger chain-verifies).
- Sessions expire (default 24h, `ttl_seconds` on create, bounded by
  `SESSION_MAX_TTL_S`). An exhausted session keeps refusing until it expires
  or the payer opens a new one; expiry is swept by the gateway's cleanup loop.

## Rails

| rail | when the gateway learns the payer | cap |
|---|---|---|
| Stacks | before broadcast (gateway is the broadcaster) | **enforced** |
| Base (EIP-3009 via CDP) | before `/settle` | **enforced** |
| Stellar | after the agent already paid on-chain | recorded, never refused |

Stellar pays first and presents a tx hash, so refusing there would move money
for nothing. Its calls are counted against the session so the ledger is
complete; over-cap Stellar calls are served and logged.

## Failure rules

- Store outage during the cap check: on enforced rails the call is refused
  with `session_store_unavailable` and nothing charged — the same fail-closed
  rule the replay store already applies to paid calls. Stellar proceeds.
- A reservation that could not be released is logged at critical; the cap
  then over-counts by that amount until corrected (never under-counts).
- `session_create` itself is never reserved against a session.

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
