# AgentPay — Observability & Monitoring Plan

Date: May 15, 2026 · Revised: May 16, 2026 (after PR #13/#14/#12 shipped)
Status: Proposed — slot into ROADMAP.md Tier 2 as **D4** (parallel to D1/D2/D3) before Round 1 closes end of May.
Owner: Valeria

---

## Why this is the missing key point

The roadmap covers engineering hardening, SDK, landing, and distribution — but has no entry under any tier for "how do we know if production is healthy." The current state:

- Gateway has been live on Stellar mainnet + Base mainnet since March 31. No payment success/failure dashboard exists.
- The OZ x402 facilitator returns 401 on every Stellar verification (documented behaviour since early 2026). Every Stellar payment eats the dead timeout before falling through to Horizon. PR #18 flag-gated the facilitator to default disabled, removing the 15s loss on the common path — but if anyone ever flips it back on, **no one is timing it**. Need a histogram.
- `split_payment(...)` to the developer's wallet happens after `return` — if it fails, the agent's call succeeds and 85% of the revenue is lost silently. PR #14 added the `split_done` state transition that *could* surface this analytically (rows stuck at `verified`/`payment_done` without `split_done` upstream of `payment_done` = split failed). **No alert yet.**
- The keepalive loop pings `/health` every 5 minutes to dodge Railway cold-start. If it dies, the symptom is "first user of the morning times out" — discovered by user, not by us.
- For SCF and Instaward, we have no numbers to put in the deck: payments processed, unique agents, success rate, p95 verification latency, $ settled by network.

What PR #13/#14/#12 already gave us — and what this plan now builds on:

- **`payment_logs` is the system of record.** Every challenge from issuance through one of seven terminal states (`payment_done`, `abandoned`, `rejected`, `refund_done`, `refund_failed`, plus the transient `pending` / `verified` / `split_done` / `refund_pending`) is captured with `payment_id`, `tool_name`, `network`, `agent_address`, `tx_hash`, `client_ip`, `user_agent`, `gateway_fee_usdc`, `developer_address`, `refund_tx_hash`, `error_reason`, `refund_attempts`, `created_at`, `updated_at`. The schema and lifecycle exist; we just need to *read* from them.
- **Replay state is on Supabase, not in process memory.** The `_completed_payments` / `_used_base_tx_hashes` in-memory sets are now graceful-degradation caches with boot-time hydration from the last hour of `replay_payment_ids` + `replay_tx_hashes`. The "unobserved restart window" no longer exists.
- **A 129-test CI suite gates every PR.** Regression surface is locked in before any observability code lands; we can refactor freely.

Round 1's D2 distribution sprint depends on telemetry that doesn't exist. Build the floor before more surface area lands.

---

## Goals (in priority order)

1. **Know within 5 minutes** when a payment-path regression hits production.
2. **Per-payment audit trail** queryable by `payment_id`, `tx_hash`, or `agent_address` — already exists in `payment_logs`; just needs to be exposed.
3. **Public stats page** with payment volume + tool usage that doubles as fundraising narrative ("X agents made Y payments totalling $Z USDC last 30 days").
4. **On-call runbook** — when an alert fires at 3am, a non-Valeria human can triage from the runbook alone.

Explicit non-goals: full distributed tracing, custom dashboards beyond what Grafana Cloud free tier renders, paid APM. We stay on free tiers until revenue says otherwise.

---

## Phase 0 — Wire metrics into the lifecycle PATCH sites (pre-Phase-1, free)

Almost-free instrumentation because the data flow already exists. PR #14's state machine PATCHes are the natural emit points:

- Inside `routes/tools.py:call_tool`, every awaited `update_payment_log_state(payment_id, "payment_done", ...)` is also a metric increment.
- Same for `rejected`, `refund_pending`. Fire-and-forget intermediates (`verified`, `split_done`) emit too.
- Inside `stellar.py:send_refund` — terminal success/failure increments the refund-result counter.

No new background polling. No Supabase round-trips for metrics. The metrics increment in the same code path that's already writing to Supabase, so they're always in sync with the lifecycle state.

This step is technically part of Phase 1 below, but worth calling out as the design point: **metrics emission is co-located with state transitions, not retro-fitted from a separate poller.**

---

## Phase 1 — Quick wins (3 days, this week)

Three integrations, free tiers, zero infra to run.

### 1.1 Sentry for error tracking
- `sentry-sdk[fastapi]` in `requirements.txt`.
- `sentry_sdk.init(...)` in `gateway/main.py:lifespan`, behind a `SENTRY_DSN` env var (no-op if unset, same pattern as `SUPABASE_URL`).
- Wrap `verify_payment` (Stellar + Base) and `split_payment` and `send_refund` in explicit `try/except` that calls `sentry_sdk.capture_exception(...)` — these are the silent-failure surfaces.
- Tag every event with `network` (stellar-mainnet / stellar-testnet / base-mainnet), `tool`, and `payment_id` (`set_tag` + `set_context`).
- Replace the silent `except: pass` patterns in `agent/_wallet.py` (the agentpay-x402 SDK) with `logger.warning` + Sentry breadcrumb. Two birds, one PR.

### 1.2 Structured logging with payment_id correlation
- Switch the existing `logging.basicConfig` format to JSON via `python-json-logger`.
- Add a `LoggerAdapter` that injects `payment_id` and `agent_address` into every log line inside a payment request's lifecycle (set at the `call_tool` route entry, propagated via `contextvars`).
- Railway log explorer becomes grep-able by ID without scraping.

### 1.3 `/metrics` Prometheus endpoint
- `prometheus-fastapi-instrumentator` exposes a `/metrics` endpoint with HTTP latency, status codes, and request volume out of the box.
- Wire the **Phase 0** custom counters/histograms here. Names below mirror the `payment_logs.state` values so it's trivial to cross-check Prometheus against Supabase analytics.

Counters:
- `agentpay_lifecycle_transitions_total{tool, network, state}` — every transition to `pending` / `verified` / `split_done` / `payment_done` / `rejected` / `abandoned` / `refund_pending` / `refund_done` / `refund_failed`. Single counter family, slice in Grafana.
- `agentpay_refund_attempts_total{tool, network, result}` — `result` ∈ `{success, op_no_trust, horizon_timeout, gateway_secret_not_configured, max_attempts, other}`. Driven from the worker loop in `main.py:_refund_worker_loop`.

Histogram:
- `agentpay_verify_duration_seconds{network, path}` where `path` ∈ `{facilitator, horizon_rpc, base_jsonrpc}` — finally meters the OZ facilitator path (now flag-gated by PR #18 but still configurable per env). One emit per `verify_payment` invocation.

Grafana Cloud free tier scrapes `/metrics` via their hosted Prometheus, no infrastructure to run.

**Acceptance for Phase 1**: a synthetic broken payment in testnet shows up in Sentry within 60 seconds; `/metrics` returns the three custom series; one Grafana dashboard renders payment success rate + p95 verify latency split by network.

---

## Phase 2 — Alerting (1 day, late May)

The data backbone is already in Supabase + Prometheus from Phase 1. This phase is *just* alerting rules on top.

### 2.1 Grafana alert rules → Discord webhook

Wire alerts on these conditions (all firing into a single Discord webhook for now, email later):

- **Payment success rate < 95% over 15-min window per network.** Rule: `sum(rate(agentpay_lifecycle_transitions_total{state="payment_done"}[15m])) / sum(rate(agentpay_lifecycle_transitions_total{state=~"pending|verified"}[15m])) < 0.95`. Per `network` label.
- **p95 `agentpay_verify_duration_seconds{path="facilitator"}` > 10s for 5 min.** Canary for OZ facilitator behaviour change. Fires only when the facilitator is enabled.
- **Any `rejected` transition rate > 0 over a 5-min window.** Replay attempts or forged headers — both are abuse signals, want to know fast.
- **Any `refund_failed` transition.** Means the auto-refund worker gave up after 5 attempts. Manual reconciliation needed.
- **`up{job="agentpay"}` == 0 for > 2 min.** Gateway dead or `/metrics` unreachable.
- **New Sentry issue in `gateway.stellar` or `gateway.base` modules.** Independent path from Grafana — Sentry fires the Discord webhook directly.

### 2.2 Supabase-side alerts (lower priority, optional)

For state-machine invariants that don't have a clean Prometheus query:

- Rows stuck at `verified` or `split_done` for > 5 min — means the state machine got interrupted mid-flight (Railway worker died between the awaited `payment_done` PATCH and the next fire-and-forget split write). Currently zero in production after the PR #14a state-guard fix; alert at any non-zero count.
- Rows in `refund_pending` with `refund_attempts >= 5` that haven't transitioned to `refund_failed` — means the worker is dead. Alert via a Supabase scheduled function (or fold into the weekly digest).

**Acceptance for Phase 2**: kill testnet gateway → alert in Discord within 3 min. Force a payment with garbage memo → Sentry issue + `payment_logs` row with `state='rejected'` and `error_reason='Invalid X-Payment header format'`. Force a tool failure on a `dune_query` with no DUNE_API_KEY → `refund_pending` row + Discord alert if it never resolves.

---

## Phase 3 — Public stats + runbook (3 days, early June)

Turn the data into a fundraising and trust surface.

### 3.1 `agentpay.tools/stats` public dashboard
Built once `agentpay.tools` resolves (D3 dependency). Single page, server-rendered.

- 30-day payment volume (USD)
- Payments by tool (top 5)
- Payments by network (Stellar vs Base split)
- Unique agents (last 7d / last 30d)
- Verification success rate (overall + per network)
- p95 latency badge
- Conversion ratio (paid / pending) per tool — the analytics the PR #14 lifecycle was designed for

Data source: a `payment_stats_public` Postgres view over `payment_logs` that hides `agent_address`, `tx_hash`, `refund_tx_hash`, `client_ip`, `user_agent`, and `developer_address`. RLS allows anonymous SELECT on the view but not the underlying table.

This page is also the SCF/Instaward leave-behind — same URL goes in both grant updates. If D3 DNS is still pending when Phase 3 starts, ship at `gateway-production-2cc2.up.railway.app/stats` as an interim.

### 3.2 On-call runbook
Use the `operations:runbook` skill to produce `RUNBOOK.md`. Required entries:

- "Stellar payments suddenly all failing" → check Horizon RPC status, check USDC trustline on gateway wallet, check `STELLAR_FACILITATOR_ENABLED` flag, query `payment_logs WHERE state IN ('rejected', 'refund_pending') AND created_at > now() - interval '15 min'` for error_reason patterns.
- "Base payments suddenly all failing" → check `mainnet.base.org` JSON-RPC, check Base gateway wallet ETH balance for gas.
- "Refunds piling up at refund_failed" → query `WHERE state='refund_failed' AND created_at > now() - interval '1 day'`, group by `error_reason`. If `op_no_trust` dominates → agents missing USDC trustline (document for SDK). If `gateway_low_on_xlm` → top up gateway wallet. Manual reconciliation procedure for individual rows.
- "Gateway returning 500s on all calls" → Railway status, redeploy, check Supabase health (`SELECT 1 FROM payment_logs LIMIT 1` via SQL editor).
- "Replay attempts detected" → query `WHERE state='rejected' AND error_reason ILIKE '%replay%'`, audit `agent_address`, consider IP block via `slowapi`.
- "Boot-time replay hydration empty when it shouldn't be" → check `_hydrate_replay_state_from_supabase` log line on startup, check Supabase reachability from gateway pod.

Each entry: symptom, first check (one command), diagnostic queries against `payment_logs`, fix, escalation contact.

### 3.3 Weekly Discord digest (scheduled task)
Auto-post to AgentPay Discord every Monday 09:00 UTC:

> Last week: N payments, $X.XX settled, top tool = `funding_rates`, success rate 99.2%, p95 verify latency 1.4s, refund rate 0.0%.

Same numbers that go in the SCF update — built once, posted forever. Use `mcp__scheduled-tasks__create_scheduled_task` for now; if it proves load-bearing, fold into a 4th background loop in `lifespan` (alongside `_cleanup_loop`, `_abandoned_sweep_loop`, `_refund_worker_loop`).

---

## What ships where in the codebase

| Concern | File | Phase |
|---|---|---|
| Sentry init | `gateway/main.py:lifespan` | 1 |
| Sentry breadcrumbs / capture | `gateway/stellar.py`, `gateway/base.py` (around `verify_payment`, `split_payment`, `send_refund`) | 1 |
| Structured logging adapter | `gateway/_logging.py` (new) | 1 |
| `/metrics` endpoint | `gateway/routes/metrics.py` (new) | 1 |
| Lifecycle counter emission | `gateway/routes/tools.py` (co-located with each `update_payment_log_state` call) | 1 |
| Refund counter emission | `gateway/main.py:_refund_worker_loop` (already the per-row decision point) | 1 |
| Verify-duration histogram | `gateway/stellar.py:_verify_payment_horizon` + `gateway/base.py:verify_base_tx` | 1 |
| Grafana alert rules + Discord webhook | dashboard config (no code) | 2 |
| Public stats endpoint | `gateway/routes/stats.py` (new) | 3 |
| Public stats UI | inline HTML in `routes/stats.py` or static `gateway/static/stats.html` | 3 |
| Runbook | `/RUNBOOK.md` | 3 |
| Weekly digest task | `mcp__scheduled-tasks__create_scheduled_task` (no code, or a 4th `lifespan` loop) | 3 |

`gateway/services/supabase.py` is touched only if we want to add a `query_recent_transitions()` helper for the weekly digest; everything Phase 1/2 reads is via existing `payment_logs` SELECTs that the gateway already does.

---

## Cost & dependencies

- Sentry free tier: 5k errors/month — plenty at current volume.
- Grafana Cloud free: 10k series, 14-day retention — plenty.
- Supabase: already in stack, `payment_logs` already at production scale.
- Discord webhook: free.
- **Total recurring cost: $0/month** until we outgrow free tiers.

Dependencies on existing roadmap:
- Phase 3 stats page depends on D3 `agentpay.tools` DNS resolution (already in Round 1 scope). Falls back to the existing Railway hostname if D3 slips.
- No other blocking dependencies — the data backbone (`payment_logs` lifecycle, replay state in Supabase) is already in production.

---

## Proposed slot into ROADMAP.md

Add as **Tier 2 — D4: Observability & telemetry** in Round 1, alongside D1/D2/D3.

Items now satisfied (strike from future tiers):
- The original Tier 3 "Move replay state to Supabase" — done by PR #13e (cutover).
- The original Tier 3 `payment_logs` table — done by PR #13a/#14 (lifecycle state machine).
- The original Tier 4 "auth-gate `/stats` or strip agent_address from public dashboard" — solved by the `payment_stats_public` view in Phase 3.

Net roadmap impact: +1 new tier-2 item, -3 future items now redundant.

---

## Revised scope estimate (post-PR #13/#14/#12)

Originally the plan was 11-15 focused days. With the Supabase data backbone and lifecycle state machine already shipped, the revised total is **~5-7 focused days**:

- **Phase 1: ~3 days (~6-8h focused work).** Sentry init + structured logging + `/metrics` + three custom series + one Grafana dashboard. The metric emission piggy-backs on existing lifecycle PATCH sites — no separate poller.
- **Phase 2: ~1 day (~4h).** Six Discord alerts wired to Grafana rules. No data-side work needed.
- **Phase 3: ~3-4 days (~8-10h).** Stats page + runbook + weekly digest. Blocked on D3 DNS but ships at the interim Railway URL if needed.

PR #13/#14/#12 pre-bought roughly half of the original Phase 2 scope.

---

## Out of scope

- Distributed tracing (OpenTelemetry, Tempo) — defer until > 10 services or > 100 req/s.
- Paid APM (Datadog, New Relic) — defer until revenue covers it.
- Real-time anomaly detection — Phase 2 alerting rules are good enough at current volume.
- Per-agent fraud scoring — separate piece of work; the data is in `payment_logs.agent_address` whenever we want it.
- Schema redesign of `payment_logs` to add separate `verify_result` / `split_result` / `api_result` columns (the original v1 of this doc proposed this). The state-machine column we shipped covers the same analytics with one column instead of three, and the redesign would be a breaking migration. Stick with what's deployed.
