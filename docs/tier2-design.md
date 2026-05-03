# AgentPay Tier 2 — Production Hardening Design

Status: **Draft** · Author: romudille-bit · Last updated: April 25, 2026

This document specifies the second wave of work on the AgentPay gateway after
the initial public release. It exists to make three product calls explicit
before any code lands — refund semantics, persistence, and test discipline —
and to sequence the implementation so each piece earns its keep on the way
in. It also doubles as the technical core of the Instaward grant ask.

The hackathon judges flagged two things in their feedback: incomplete code
paths and lack of architectural depth. Tier 1 (already shipped — see commits
`fix: tier 1 gateway cleanup …` and `refactor: dedupe wallet module …`)
addressed the first half by closing seven concrete bugs. Tier 2 addresses
the second half by deciding what the system promises when things go wrong,
persisting the state that proves those promises, and writing the tests that
keep them holding.

---

## 1. Refund semantics on tool failure

### 1.1 The problem

The current gateway has three failure modes that all silently keep the
agent's payment:

1. `_real_tool_response` raises an exception → main.py returns
   `{"error": "..."}` to the agent in the 200 body. Payment captured, split
   already fired, no data delivered.
2. The proxy/upstream returns `ConnectError` → main.py falls back through
   alternate APIs, but if all fall through the agent gets `{}` and the split
   has already fired.
3. The proxy returns a non-2xx the gateway can't handle → main.py returns
   502, but the split has already fired in the previous statement.

In all three cases the agent paid, the developer got 85%, the gateway got
15%, and the agent got nothing. That's the worst version of the trust story
the protocol is supposed to tell.

### 1.2 The three options

**A. Synchronous on-chain refund.** On failure, the gateway sends USDC back
to the agent before returning a response. Cleanest fairness story but worst
ergonomics: a second on-chain transaction on the critical path adds 5–10s of
latency to every failed call, the gateway pays the refund's network fee,
and the refund itself can fail (RPC blip, gateway low on XLM). Replay
protection gets harder because the refund tx_hash is a new unknown.

**B. Credit-on-next-call.** Failure stored as a credit on the agent's wallet
in Supabase, applied to the next 402 challenge. Fast (no on-chain second
op) and produces a clean audit trail. Three problems: agents that never
come back lose their money, cross-network credits (paid in Stellar, return
in Base?) need a conversion rule, and the credit balance becomes a database
liability the gateway has to honour through any future schema migration.

**C. Pre-split rollback with async refund.** The split is gated on tool
success. On failure, the split is skipped, a `refund_pending` row is
inserted into Supabase, and a background task attempts the on-chain refund
with capped retries. The agent's response is fast (the refund doesn't block
it) and carries an explicit `payment_status: "refund_pending"` field so the
agent SDK can present the correct UX. Permanent refund failures alert ops
and become a sweep job.

### 1.3 The decision

**Adopt Option C** as the canonical rule, with refund as the only commitment
the gateway makes on failure (no credits, no escrow contracts).

The rationale: Option A is the right semantics on paper but wrong on the
critical path. Option B trades on-chain simplicity for off-chain liability,
and the cross-network case (Stellar payment failing on a tool that fell
back to a Base-priced provider, for instance) doesn't have a clean answer
without a conversion oracle. Option C keeps the agent's response fast,
keeps the on-chain story clean (one payment in, one refund out, both
hashable), and only requires Supabase as the durable queue — which we
already need for replay state in §2.

The agent SDK contract becomes:

```python
result = session.call("token_price", {"symbol": "ETH"})
if result["payment_status"] == "ok":
    use(result["result"])
elif result["payment_status"] == "refund_pending":
    log_warn(f"refund queued, tx will appear within ~60s")
elif result["payment_status"] == "refund_failed":
    escalate("manual sweep needed")
```

### 1.4 What "tool failure" means

The gateway treats any of the following as a failure that triggers
rollback:

* `_real_tool_response` raised an exception (caught at the call boundary).
* Upstream API returned 4xx or 5xx after exhausting the fallback chain.
* Proxy `ConnectError` after exhausting fallbacks.
* Tool returned a structured `{"error": "..."}` blob (success-shaped but
  empty).

What does **not** trigger a refund:

* Agent sent malformed `X-Payment` header (not a payment, no payment to
  refund).
* Replay attempt (payment was never accepted).
* Expired challenge (no payment was ever captured).
* Budget exceeded on the agent SDK side (pre-payment, agent's own check).

### 1.5 The settlement state machine

```
        ┌─────────────────────┐
challenge_issued ──pay──→ verified ──split_queued──→ split_done
        │                     │                          │
        └──expired/replay     │                          ↓
                              │                     payment_done
                              │
                              └──tool_fail──→ refund_pending
                                                    │
                                                    ├─sent──→ refund_done
                                                    └─fail──→ refund_failed
                                                                  │
                                                                  └─manual_sweep
```

Every transition writes a `payment_logs` row update (§2.2). No state is
in-memory only.

---

## 2. Persistence schema

### 2.1 Why

Five pieces of state currently live in Python sets/dicts and die with the
gunicorn worker: `_completed_payments`, `_used_base_tx_hashes`,
`_pending_challenges`, `_FAUCET_IP_LOG`, and the implicit "did the split
succeed?" knowledge inside `asyncio.create_task`. Replay protection
literally evaporates on every Railway redeploy.

Supabase already has the project — the schema below replaces every set/dict
with a table.

### 2.2 Tables

**`payment_logs`** — the main settlement audit trail. One row per challenge.

| column                | type        | notes                                                  |
|-----------------------|-------------|--------------------------------------------------------|
| `id`                  | uuid PK     | matches the `payment_id` issued in the 402 challenge   |
| `tool_name`           | text        | resolved tool name (post-alias)                        |
| `network`             | text        | `stellar-mainnet`, `stellar-testnet`, `base`, `base-sepolia` |
| `agent_address`       | text        | payer's wallet                                         |
| `amount_usdc`         | numeric     | full amount the agent paid                             |
| `gateway_fee_usdc`    | numeric     | 15% cut, populated when split fires                    |
| `developer_address`   | text        | nullable; developer wallet for split target            |
| `tx_hash`             | text        | the agent's settlement tx; unique constraint           |
| `state`               | text        | one of `verified`, `split_done`, `payment_done`, `refund_pending`, `refund_done`, `refund_failed` |
| `refund_tx_hash`      | text        | nullable; populated when refund settles                |
| `error_reason`        | text        | nullable; populated on failures and refunds            |
| `created_at`          | timestamptz | `default now()`                                        |
| `updated_at`          | timestamptz | trigger-bumped on every UPDATE                         |

Indexes: `(tx_hash)` UNIQUE, `(state, created_at)` for sweep queries,
`(agent_address, created_at desc)` for support lookups.

**`replay_payment_ids`** — payment_id replay guard. UUID side of the lock.

| column        | type        | notes                                  |
|---------------|-------------|----------------------------------------|
| `payment_id`  | uuid PK     | matches `payment_logs.id`              |
| `consumed_at` | timestamptz | `default now()`; insert-only           |

**`replay_tx_hashes`** — on-chain hash replay guard. Hash side of the lock.

| column         | type        | notes                                                |
|----------------|-------------|------------------------------------------------------|
| `tx_hash`      | text PK     |                                                      |
| `network`      | text        | so sepolia and mainnet hashes don't collide          |
| `consumed_at`  | timestamptz | `default now()`                                      |

We key replay protection on **both** payment_id and tx_hash because either
alone is exploitable: a replay of `(payment_id=A, tx_hash=B)` against
`(payment_id=A, tx_hash=C)` would slip past the tx-hash guard, and a replay
of `(payment_id=B, tx_hash=A)` would slip past the payment-id guard. Both
inserts run in the same transaction; one rejection rolls the other back.

**`pending_challenges`** — replaces the in-memory `_pending_challenges`
dict, keeps challenges across deploys.

| column               | type        | notes                                              |
|----------------------|-------------|----------------------------------------------------|
| `payment_id`         | uuid PK     |                                                    |
| `tool_name`          | text        |                                                    |
| `amount_usdc`        | numeric     |                                                    |
| `gateway_address`    | text        |                                                    |
| `developer_address`  | text        |                                                    |
| `request_data`       | jsonb       | original request, replayed after payment           |
| `expires_at`         | timestamptz |                                                    |
| `created_at`         | timestamptz | `default now()`                                    |

A scheduled job deletes rows where `expires_at < now() - interval '1 hour'`.

**`faucet_ip_log`** — replaces `_FAUCET_IP_LOG`, enforces cooldown.

| column      | type        | notes                                |
|-------------|-------------|--------------------------------------|
| `ip`        | inet PK     |                                      |
| `last_used` | timestamptz | `default now()`                      |

Cooldown query: `WHERE last_used > now() - interval '10 minutes'` rejects.

### 2.3 Why not separate per-network tables

Putting Stellar and Base into the same `payment_logs` keeps the audit trail
queryable in one shot ("last 24h of failed payments by network") and
matches the gateway's existing one-handler-per-tool pattern. The `network`
column is enough discriminator. The replay tables stay split because
collision risk is real enough (a Base tx hash and a Stellar tx hash are
both 64-hex strings).

---

## 3. Sequenced implementation plan

The order below is chosen so that no PR depends on something that hasn't
landed yet, and so that the riskiest commits happen on top of test
infrastructure.

| # | Item | Est | Depends on | Why this order |
|---|------|-----|-----------|----------------|
| 1 | **PR #5b** — clean gateway imports, drop `sys.path.insert` shim | 1h | – | Unblocks every later PR by making `gateway.x402` etc. importable from new files. Risk-isolated: deploys are absolute already. |
| 2 | **PR #2** — split `main.py` (2,237 lines) into `routes/` + `services/` | 2h | #5b | All Tier 2 logic lands in the new modules, not in the old 2k-line file. Doing this AFTER #5b means new files need no path hacks. |
| 3 | **#15** — pytest scaffolding + happy-path tests for `x402.py`, `base.py`, `stellar.py` | 3h | #2 | Foundation. Every later PR is gated on these tests passing. Mock Horizon and JSON-RPC; no live credentials. |
| 4 | **CI** — GitHub Action runs tests on PR | 30m | #15 | Locks in the regression surface before any behaviour change ships. |
| 5 | **#13** — Supabase tables + ORM client (no behaviour change yet) + FastAPI lifespan migration | 2.25h | #15 | Tables created, client written, but `_pending_challenges` etc. still primary. Dual-write phase. While `_startup` is being rewritten to hydrate replay state from Supabase, also migrate `@app.on_event("startup")` → `lifespan` context manager — same function, same PR, removes the FastAPI deprecation warning. ~20 added lines. |
| 6 | **#16** — async safety: wrap three Stellar SDK calls in `asyncio.to_thread` | 30m | #15 | One-line touches per call site; tests catch any regressions. |
| 7 | **#13 cutover** — flip Supabase to primary, drop in-memory dicts | 1h | #5, #15 | After dual-write soak time. Includes the boot-time hydration if Supabase is unreachable. |
| 8 | **#14** — `payment_logs` lifecycle + split task tracking + 402 challenge logging | 2.5h | #13 cutover | Insert `pending` row *before* the 402 returns, then update through `verified` → `split_done` → `payment_done` (or `abandoned` after TTL). The pre-402 insert also closes the analytics gap surfaced in §5 — every challenge is captured whether or not payment follows. Requires §2.2 schema. |
| 9 | **#12** — refund semantics + state machine | 4h | #14 | The big one. Implements Option C from §1. Lands behind a feature flag (`REFUND_ENABLED`) so we can dark-launch. |
| 10 | **#19** — schema-validate CDP Mode A response | 30m | #15 | Add `assert "transaction" in data and len(data["transaction"]) == 66` etc. Integration test stub for the response shape. |
| 11 | **#17** — Stellar facilitator fallback covers all non-200 cases | 30m | #15 | One condition change in `stellar.py:154-207`. Tests cover the fallback path. |
| 12 | **#18** — flag-gate or delete OZ branch | 1h | #17 | After #17 confirms Horizon-only path is bulletproof. Default `STELLAR_FACILITATOR_ENABLED=false`. |

Total estimate: ~18 hours of focused work, sequenced across two weeks.

The split between "land the test foundation" (steps 1–4) and "do the
behaviour changes" (steps 5–12) is deliberate — the first half is invisible
from the outside but doubles the speed of the second half.

---

## 4. Scope for the Instaward grant ask

Of the items above, the grant case is strongest when scoped to the
production-readiness story rather than the cleanup story. Refactors and
import hygiene aren't grant material. The following five items are.

* **#12 — Refund semantics on tool failure.** The signature deliverable.
  Demonstrates that AgentPay treats failed paid calls as a first-class
  protocol concern, not an edge case.
* **#13 — Persisted replay state.** Replaces process-local memory with
  Supabase. Eliminates the redeploy-erases-replay-protection class of
  vulnerability.
* **#14 — Payment audit trail.** `payment_logs` becomes the public artefact
  developers can query to verify the gateway behaved correctly on their
  payment.
* **#15 — Tests + CI.** The "is this code we can trust?" answer for any
  reviewer. Even 30% coverage on the payment paths shifts the narrative.
* **#16 — Async safety.** A two-hour fix that prevents 5–10s event-loop
  stalls under any concurrency. Cheap, important, easy to demo with a
  load test.

Out of scope for the grant ask, kept on the engineering roadmap:

* PR #5b, PR #2 — internal refactors, prerequisite for the grant work but
  not the deliverable.
* #17, #18 — facilitator hygiene; ship with the rest but not the headline.
* #19 — CDP schema validation; defensive code, not a feature.

The grant deliverable is one paragraph: **"AgentPay v0.2 makes the
payment-on-failure contract explicit, persists every settlement to
Supabase, and ships with a CI-gated test suite that protects the
guarantees in production. The funded work converts a hackathon-grade
gateway into infrastructure other developers can build on without holding
their breath on every redeploy."**

---

## 5. 402 challenge logging (post-discovery, April 29)

### 5.1 What we found

After tonight's Supabase rescue, a quick look at Railway access logs
surfaced a steady stream of `POST /tools/{name}/call → 402` events that
never follow through to payment:

```
POST /tools/yield_scanner/call  → 402   (no follow-up POST → 200)
POST /tools/funding_rates/call  → 402   ← funding_carry tool!
POST /tools/token_price/call    → 402
POST /tools/defi_tvl/call       → 402
```

This is the "interest without conversion" signal — agents probing prices,
scrapers crawling the gateway, real users who got the challenge but didn't
pay. The data exists only as transient Railway access log entries that
scroll off after retention; nothing in `payment_logs` captures it.

The miss matters more than it first appears. On testnet alone we've
already lost visibility on:

* 12 dormant faucet wallets (got funded, never called) — were they probing
  endpoints first and giving up, or never trying?
* The funding_carry funnel — `funding_rates` shows up in the 402 traffic,
  meaning someone is *looking* at the condor-carry toolset without paying.
  We can't tell if it's the same caller as the dormant faucets.
* Tool-level interest patterns. Some tools may attract many probes for
  every paid call, others may be a clean 1:1 — that ratio is a pricing
  signal we currently can't see.

### 5.2 Why this folds into #14, not a separate table

The first instinct is "create a `challenge_logs` table, fire-and-forget
log on every 402." That's the wrong shape for two reasons:

1. **Duplicates state.** A challenge that *does* result in payment would
   exist in two places (`challenge_logs` row + `payment_logs` row) joined
   by `payment_id`. Every analytics query becomes a JOIN. Eventually one
   table wins and the other gets migrated away.

2. **Re-introduces fire-and-forget fragility.** This is exactly the
   pattern that just bit us with `log_payment` — fire-and-forget +
   logger.warning means writes silently fail and nobody notices for 25
   days. The right pattern is the same one #14 uses for payments:
   pre-write a row *before* the 402 response, and let later state
   transitions update it.

### 5.3 The unified design

`payment_logs` becomes the system of record for every challenge from
issuance through one of three terminal states:

```
   POST /tools/X/call  ──insert row──→  pending
                                          │
                       ┌──────────────────┼──────────────────┐
                       │                  │                  │
                  payment received    TTL expires        replay/forged
                       │                  │                  │
                       ↓                  ↓                  ↓
                   verified          abandoned             rejected
                       │
                  split fired
                       │
                       ↓
                   split_done
                       │
                  tool returned
                       │
                       ↓
                  payment_done
```

Schema additions to §2.2's `payment_logs`:

| change | purpose |
|---|---|
| `state` column gains values `pending`, `abandoned`, `rejected` (in addition to the existing `verified`, `split_done`, `payment_done`, `refund_pending`, `refund_done`, `refund_failed` from §1.5) | Captures the full challenge lifecycle. `pending` is the initial insert; `abandoned` is set by a sweep job for rows where `state="pending"` and `created_at < now() - interval '5 minutes'`; `rejected` covers replay attempts and malformed requests. |
| Existing `agent_address`, `amount_usdc`, `tx_hash` columns become **nullable** | At `pending` insert time, only `payment_id`, `tool_name`, `network`, and price are known. Agent address arrives with the payment header in step 2; tx hash arrives during verification. |
| New optional `client_ip` column (inet) | Useful for funnel analytics and abuse detection. Only populated for routes that have it (faucet does, /tools/X/call doesn't currently — but adding `request.client.host` to the route handler is one line). |
| New optional `user_agent` column (text) | Same rationale. Distinguishes human curl from agent SDK from MCP server. |

### 5.4 Implementation hook

Inside `routes/tools.py:call_tool`, just before returning the 402
response:

```python
# Synchronous insert — must succeed before the 402 returns. If Supabase
# is unreachable, fail closed with a 503 so the gateway never issues
# challenges it can't track. (See §6 Open Questions for the soft-fallback
# alternative.)
await log_pending_challenge(
    payment_id=challenge.payment_id,
    tool_name=resolved,
    network=settings.STELLAR_NETWORK,
    amount_usdc=tool.price_usdc,
    client_ip=request.client.host if request.client else None,
    user_agent=request.headers.get("user-agent"),
)
```

Then later in the same handler, when payment verifies:

```python
await update_challenge_state(payment_id, state="verified",
                             agent_address=agent_address, tx_hash=tx_hash)
# ...split fires...
await update_challenge_state(payment_id, state="split_done")
# ...tool returns 200...
await update_challenge_state(payment_id, state="payment_done")
```

A separate background sweep (~Celery or a periodic asyncio task)
transitions stale `pending` rows to `abandoned`:

```sql
UPDATE payment_logs
   SET state = 'abandoned', updated_at = now()
 WHERE state = 'pending'
   AND created_at < now() - interval '5 minutes';
```

### 5.5 What this unlocks

Direct analytics queries that are currently impossible:

```sql
-- Tools by interest:payment ratio (funding_carry signal!)
SELECT tool_name,
       COUNT(*) FILTER (WHERE state = 'pending')      AS pending,
       COUNT(*) FILTER (WHERE state = 'abandoned')    AS abandoned,
       COUNT(*) FILTER (WHERE state = 'payment_done') AS paid,
       ROUND(100.0 * COUNT(*) FILTER (WHERE state = 'payment_done')
                   / NULLIF(COUNT(*), 0), 1) AS conversion_pct
  FROM payment_logs
 WHERE created_at > now() - interval '7 days'
 GROUP BY tool_name
 ORDER BY paid DESC;

-- Faucet → first paid call funnel
SELECT a.created_at::date AS faucet_day,
       COUNT(DISTINCT a.recipient) AS wallets_funded,
       COUNT(DISTINCT p.agent_address) AS wallets_paid
  FROM faucet_log a
  LEFT JOIN payment_logs p
    ON p.agent_address = a.recipient
   AND p.state = 'payment_done'
 GROUP BY 1 ORDER BY 1 DESC;
```

Both queries answer questions we couldn't even formulate before this
schema. The conversion-by-tool query in particular tells us whether
specific tools (e.g., `funding_rates`) are oversold by docs vs. the value
they actually deliver — a real product signal.

### 5.6 Sequencing

This work is **scoped into #14**, not a separate PR. #14 was a 2-hour
task; the additions in §5.3-§5.4 push it to ~2.5 hours. The reason it
stays bundled: separating "log payments" from "log challenges" creates an
artificial boundary in the same handler, and a single PR keeps the
schema decision intact (no migration step).

---

## 6. Open questions

* **Refund retry policy.** How many attempts before `refund_failed` and
  what's the backoff? Suggest 5 attempts with exponential backoff capped
  at 30 minutes total wall-clock.
* **Refund gas economics.** Each failed Base call costs the gateway a
  refund tx (~$0.01 at current Base mainnet gas). A run of failures could
  cost real money; is there a circuit breaker that pauses the affected
  tool after N consecutive failures?
* **Supabase outage behaviour.** If Supabase is unreachable at challenge
  issue time, does the gateway fail closed (return 503) or fall back to
  in-memory? Suggest fail-closed with a 60-second cache for verified
  payments to ride out brief outages.
* **Migration story for in-flight payments.** When #13 cuts over, what
  happens to challenges that were issued under the in-memory regime?
  Suggest a 5-minute drain window where both stores are read.

* **FastAPI lifespan migration (bundled into #13).** Surfaced during
  the test runs after PR #16 — `@app.on_event("startup")` is deprecated
  in favour of FastAPI's lifespan async context manager pattern. The
  migration is ~20 lines and changes nothing functionally, but
  `_startup` is going to be substantially rewritten for #13 anyway
  (Supabase replay-state hydration, dual-write setup, eventual cutover).
  Making the lifespan switch in the same PR avoids touching the
  function twice. New shape:

  ```python
  from contextlib import asynccontextmanager

  @asynccontextmanager
  async def lifespan(app: FastAPI):
      # current _startup body (Supabase fetch, hydrate replay state)
      yield
      # shutdown — currently nothing, future home of background-task drain

  app = FastAPI(..., lifespan=lifespan)
  ```

  TestClient handles both `on_event` and `lifespan` transparently, so
  no test changes required. The two slowapi `iscoroutinefunction`
  warnings observed alongside this one are upstream library issues
  and resolve when slowapi ships its next release.

---

*This doc is intentionally narrower than a full v2 architecture review —
Soroban escrow, multi-chain expansion, and tool-developer onboarding are
deferred to a separate v0.3 doc. The point of Tier 2 is to make v0.1
production-trustworthy first.*
