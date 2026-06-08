# Notes: server-side session enforcement (parked)

Status: **notes only — not built.** Captured 2026-06-02. Priority right now is
users + ease of use; monetization/enforcement comes second. Revisit when (a) a
metered/paid tool ships, or (b) the Arbitrum contracts give us a non-custodial
place to enforce.

## Current reality (what `session_create` actually does)

- `session_create` ($0.001) returns a `session_id` (UUID) and echoes `max_spend`,
  then **forgets it**. `routes/session.py:428` mints the UUID; nothing persists it.
- The `session_token` from `POST /v1/agent/register` (`routes/agent.py:90`) is
  also a throwaway UUID — never stored, never checked.
- There is **no `sessions` table**, and `payment_logs` has **no `session_id`** column.
- Tool calls (`/tools/{name}/call`) require only an address + per-call payment.
  No `session_id` is checked anywhere.
- The budget cap + receipt/ledger + multi-tool calling all live **client-side in
  the SDK** (`from agentpay import Session`, `quickstart()`), and are **free**.

So today `session_create` is a **discovery/positioning anchor** (the paid resource
that gets indexed on Coinbase Bazaar — indexing only fires when a real CDP payment
settles for it) and a placeholder for future metering. It does **not** gate any
functionality. Don't market it as a functional paywall.

## Two enforcement models

**Model A — non-custodial spend ceiling (bookkeeping).**
Each call still pays per-call on-chain (today's x402 flow). The session is a
server-side ledger; the gateway refuses calls that would breach `max_spend`. No
agent funds held. Fits the "relay, not custodian" stance. ~days of work.

**Model B — prepaid escrow.**
Agent pays a lump sum into the session; gateway debits per call, no per-call tx.
Better UX (one payment, many calls) and the natural base for metered inference —
but the gateway holds agent USDC (custodial), which contradicts the current
positioning. Non-custodial version = an **on-chain escrow + atomic-split contract**
(Stellar Soroban on the existing Tier-3 roadmap, **or the Arbitrum contracts we're
now building** — this is the reuse opportunity).

## What Model A would take (concrete build list)

1. `sessions` table (manual Supabase migration — no migration tooling in-repo):
   `session_id` PK, `agent_address`, `max_spend`, `spent` (default 0), `label`,
   `status` (active/exhausted/expired/revoked), `created_at`, `expires_at`.
   Add `session_id` column to `payment_logs` so the ledger is a simple query.
2. Persist the session row on create (`routes/session.py`, after payment verifies).
3. Accept `X-Session-Id` on `routes/tools.py:call_tool`; look it up; reject if
   missing/expired/exhausted/agent-mismatch. Gate **paid tools only** — free tools
   never require a session (keeps the zero-setup free tier intact).
4. **Atomic cap enforcement** (same TOCTOU class as the replay fix): a Postgres
   RPC `consume_session_budget` —
   `UPDATE sessions SET spent = spent + :cost WHERE session_id = :id AND spent + :cost <= max_spend RETURNING spent`.
   Zero rows → over budget → reject. PostgREST can't do this in a plain PATCH, so
   it's a small SQL function via `/rpc` (same pattern the design doc flags for
   `increment_refund_attempt`).
5. Lifecycle: expiry sweep (reuse the `_cleanup_loop` pattern) + a
   `GET /v1/session/{id}` so the agent can read the server-side ledger/remaining.
6. Dark-launch behind a `SESSION_ENFORCEMENT` flag (like `REFUND_ENABLED`).

## The question that decides if it's worth it

Server-side enforcement only adds value over the client-side cap when the **budget
owner ≠ the code spending it** (a principal sets a budget; a delegated/untrusted/
buggy agent spends; the gateway, not the agent's own code, enforces the ceiling).
That's the "spend-authorization" framing in the Bazaar tags. If delegated spend
isn't a real user scenario yet, Model A is enforcement theater.

Plus a chicken-and-egg: **nothing paid to meter yet** — all 18 tools are free, the
only paid resource is `session_create` itself. A server-enforced ceiling has
nothing to bite on until metered inference / paid tools ship. Build enforcement
*with* the thing being enforced, not before.

## Recommendation (sequencing)

1. **Now:** keep `session_create` as the Bazaar anchor; stop framing it as a gate;
   the client-side SDK cap is the honest story. Focus on users + ease of use.
2. **When the first metered/paid tool ships:** build Model A alongside it.
3. **If prepaid/escrow becomes the product:** do it as the Arbitrum (or Soroban)
   escrow + atomic-split contract — non-custodial Model B — not a gateway patch.

## Arbitrum reuse hook

The escrow/atomic-split contract being built for Arbitrum is the right home for
Model B: agent funds a session on-chain, the contract enforces the cap and splits
85/15 atomically, emits an event the gateway listens for. That makes the gateway a
relay (non-custodial) AND gives real, on-chain spend governance — the version of
"session" that's actually worth paying for. Carry the schema/RPC ideas above over
as the off-chain ledger/index that mirrors the contract state.
