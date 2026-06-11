# Design Notes

Load-bearing design decisions that aren't obvious from the code. Inline
comments state *what* invariant holds; the *why* and the history live here.

## Payment lifecycle (payment_logs state machine)

States: `pending → verified → payment_done` (happy path), with terminal
branches `rejected`, `refund_pending → refund_done | refund_failed`, and
`abandoned` (swept).

- **Terminal states are awaited; intermediate states are fire-and-forget.**
  Terminal writes (`payment_done`, `rejected`, `refund_pending`) must be
  consistent at response time — a `create_task` on the rejected branch loses
  the race because there's no downstream await before the return. The
  intermediate `verified` PATCH is fire-and-forget for latency, guarded with
  `expected_state='pending'` so a late write can't clobber a terminal state
  (without the guard, ~half of production rows stuck at `verified`).
- **The pre-402 pending INSERT is awaited and fail-closed**: the gateway
  refuses to issue challenges it can't track (503 on Supabase write failure).
- **Free ($0) tools flow through the full 402 lifecycle** — challenge,
  replay consume, receipt — but skip on-chain settlement (`free:<id>` proof).
  Do NOT re-add a free→200 short-circuit in the gateway; it breaks the
  funnel analytics that the tests pin.
- **Base settlements produce a second row keyed on tx_hash** because x402-v2
  doesn't echo the challenge UUID back; the UUID row is swept to `abandoned`.

## Replay protection: atomic consume

The fast `_is_replay` check is only a pre-check — on-chain verification has
await boundaries, so two concurrent retries of one tx_hash can both pass it.
The authoritative claim happens after verification and before fulfilment:
an in-memory check-and-add (atomic within the worker's event loop) plus an
**awaited** insert into the insert-only `replay_tx_hashes` /
`replay_payment_ids` tables (composite/PK → HTTP 409 = another worker
already consumed it → reject). `record_*` returns True when Supabase is
disabled/unreachable so infra blips never fail closed; the in-memory set
still guards the single-process case. Supabase is primary with the
in-memory structures as a warm fallback cache, hydrated at startup (without
hydration, a Supabase outage right after a deploy would silently fail open).

## Stellar verification

The OZ facilitator has returned 401 since early 2026; direct Horizon RPC is
the de facto production path (`STELLAR_FACILITATOR_ENABLED=False` skips the
wasted 15s round-trip). Horizon checks: tx success, **text memo binds to the
payment_id** (prefix match, 28-byte truncation), asset/issuer/from/to, and
amount (>2x overpay is flagged, not rejected). Agents pay their own ~0.00001
XLM network fee.

## Revenue split / refunds

`split_payment` retries with backoff, rebuilding the tx each attempt (fresh
sequence number). On exhaustion it stamps `error_reason='split_failed: …'`
WITHOUT touching `state` — split runs after the route's terminal
`payment_done` write, so clobbering state would corrupt funnel analytics.
Crash-*resilient*, not crash-*durable*: a worker death mid-retry loses that
one in-flight split (a durable `split_pending` queue is deferred — splits
are currently no-ops since all tools pay the gateway wallet). The refund
worker mirrors this pattern; refund duplication on a crash between submit
and PATCH is an accepted risk on single-pod deploys.

## Known foot-guns

- **Never add a local `import asyncio` inside `verify_and_fulfill`** — it
  shadows the module import and raises UnboundLocalError at the earlier
  `create_task` calls. A deploy once crashed every paid call this way;
  `tests/test_x402.py::TestVerifyAndFulfill` is the regression guard.
- Supabase tool rows can be partial; `registry.py` is the source of truth
  for tool existence and discovery hints, Supabase is an override layer.
  An intentionally-empty Supabase field gets shadowed by the seed.
- x402-v2 clients send the same payload in X-PAYMENT and PAYMENT-SIGNATURE;
  the gateway must route on "is X-Payment a parseable Stellar proof",
  not on header presence.
