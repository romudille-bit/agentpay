# Changelog

All notable changes to **agentpay-x402** (the `agentpay` Python SDK).
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); this
project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **`accepted` now echoes the seller's accepts entry verbatim** (AGE-90,
  HIGH — unblocks ~half the paid x402 marketplace). The X-PAYMENT envelope's
  `accepted` block was reconstructed — normalized network, stringified
  amount, clamped timeout, plus injected `resource`/`mimeType` keys. Strict
  v2 middlewares deep-compare `accepted` against their own advertised entry;
  the injected keys alone produced `"No matching payment requirements"` and
  a fresh `402 {}` on every paid retry — a stable 7-seller rejection cluster
  across two prober sweeps (ApiToll, AgentUtility, GEDX402, Agent402, JMT,
  Otto AI, kadec0), with the reason hidden in the re-challenge's
  PAYMENT-REQUIRED header, which error truncation never surfaced. Root-caused
  live 2026-07-28: echoing the entry verbatim flips the rejecting sellers
  from matcher rejection straight to signature verification. Normalization
  (CAIP-2 network, amount-key tolerance, AGE-67 timeout clamp) still applies
  to the SIGNED authorization — only the declarative echo is verbatim, which
  tolerant subset-matchers accept identically. Note: `accepted.network` now
  carries the seller's own vocabulary (e.g. `"base"`), not normalized CAIP-2.
- **GET-served x402 resources were being called with no arguments** (AGE-83,
  HIGH). When the seller's 402 declared `input.method: GET`, the SDK retried
  with `client.get(url, headers=…)` and silently dropped the caller's `params`
  — so the resource was paid for, then called with nothing. Sellers answered
  with an error or an empty body and looked like non-deliverers. Live evidence:
  `x402.shizu.me/pdf` (`GET ?url=`) scored 0.0 delivery across three paid
  Prober probes while being a working service. Params are now merged into the
  query string (`_with_query`): explicit params already in the URL win, values
  are URL-encoded, non-scalars JSON-encoded, `None`s dropped. POST resources
  are unchanged, and the signed `resource` is still the query-stripped URL, so
  signature matching is unaffected.

## [0.3.1] — 2026-07-20

Regression-fix release for the 0.3.0 budget-cap hardening, from the 2026-07-20
review follow-up (findings F1/F2/F7). No API changes.

### Fixed
- **Tight budgets no longer fail deterministically** (F1, HIGH — regression
  introduced by the AGE-53 cap in 0.3.0). The client-side cap was computed
  *after* this call's own budget hold was placed, and `remaining_usd()`
  subtracts that hold — so the cap double-counted it:
  `min(remaining_before − price, 1.05·price)`. Exact-fit budgets
  (`max_spend == price`) always raised `BudgetExceeded`, and every session
  silently stranded its last call once remaining < 2× price. The cap now adds
  the call's own hold back (`_cap_excluding_hold`), computed under a single
  lock. The fallback fit check likewise no longer counts the original hold
  (`_would_exceed_excluding_hold`). Regression tests: exact-fit succeeds,
  last-call-exhausts-budget succeeds, concurrent exact-fit loser still fails
  closed.
- **Spend booking and hold release are now one atomic locked section** (F2).
  The `finally` previously released the hold and absorbed the client log under
  two separate lock acquisitions; in the gap a concurrent `call()` saw
  inflated remaining and could over-reserve by up to one leg price. New
  `_absorb_and_release()` mirrors the URL path's `_record_spend`.
- **No duplicate receipt rows when a fallback re-reserve fails** (follow-up
  low). The failed $0 leg was absorbed once before the fallback hold swap and
  again by the `finally` if the re-reserve raised; the absorbed entries are
  now cleared from the old client's log.
- **`agentpay.__version__` now matches the published version** (F7). The
  0.3.0 wheel self-reported `0.2.7` because only `pyproject.toml` was bumped.
  Both are now `0.3.1`, and a pre-publish test
  (`test_version_matches_pyproject`) fails the suite if they ever diverge.

## [0.3.0] — 2026-07-19

Breaking payment-safety release from the 2026-07 gateway code review. Every fix
below hardens the pay path against overspend, double-pay, and hostile 402s.
Callers on `>=0.2` that relied on the budget cap silently clamping the amount
must now handle `BudgetExceeded`. This is the first release to raise it.

### Security / Fixed — Gateway Code Review 2026-07, SDK cluster (AGE-53..57)
- **Budget cap now binds the amount actually paid** (AGE-53, CRITICAL).
  `Session.call()` passes `max_spend = min(remaining budget, quoted price
  × 1.05)` into the client, which hard-fails **before** paying or signing if
  the 402 demands more. A gateway advertising $0.001 and demanding $0.50 in
  the 402 is now refused instead of paid. The Base `payment_options` amount
  is bound by the same cap before signing.
- **Spend is recorded at broadcast/auth-transmission time, not at HTTP 200**
  (AGE-54). A payment whose tool call then fails still counts against
  `spent()`/`remaining()` (call-log `state`: `paid_no_result`,
  `uncertain_settlement`, `refund_pending`). Pay-then-fail loops can no
  longer overspend the cap.
- **No fallback after funds move** (AGE-55). `Session.call()` only retries a
  fallback tool on the new typed `PrePaymentError` (nothing moved, no auth
  transmitted). Post-payment failures propagate — one `session.call()` can
  no longer pay twice.
- **Transmitted EIP-3009 authorizations are treated as potentially spent**
  (AGE-56). A non-200 after the signed auth left the wire no longer claims
  "no payment settled", never re-pays on Stellar, and records the spend as
  `uncertain_settlement`. Signing failures (pre-transmission) still fall
  back to Stellar as before.
- **`allowed_tools` / `max_per_tool` / `rate_limit` now apply to external
  x402 URLs** (AGE-57). Policy checks run in `Session.call()` before any
  routing, so URL targets (and `discover_and_call`) can no longer bypass the
  allowlist or per-tool caps.
- Server-controlled `maxTimeoutSeconds` is clamped to 600s before signing
  (AGE-67): a hostile 402 can no longer request a year-long `validBefore`.

### Changed (breaking)
- `AgentPayClient.call_tool` raises `BudgetExceeded` (was `ValueError`) when
  the 402 amount exceeds `max_spend`.
- New exported exception: `agentpay.PrePaymentError`.
- External x402 4xx rejections after auth transmission now read
  "…settlement uncertain, spend recorded…" (prober matcher updated).

### Added
- **x402 v2 `PAYMENT-REQUIRED` header fallback** (AGE-9). The SDK now reads the
  v2 header form in addition to the JSON body; an endpoint it cannot score is
  rejected rather than paid blindly.

### Concurrency / Fixed (AGE-66, AGE-68)
- **Budget is reserved before payment under a re-entrant lock** (AGE-66). Two
  concurrent `session.call()`s can no longer both clear the cap check and jointly
  overspend — each reserves its slice up front. This also fixes a double-release
  bug: a failed fallback re-reservation returned the hold twice and drove the
  reserved total negative, letting concurrent calls exceed the cap
  (regression-tested).
- **Timed-out submits are polled for their result, not blindly retried**
  (AGE-68). A slow settle that lands after the client gives up is detected on
  poll instead of triggering a second send.
- Base settlement records the **signed** amount, not the amount advertised in the
  402 body (AGE-53/56 follow-up).

## [0.2.7] — 2026-06-17

### Fixed
- **External x402 URLs now work against GET + query-param endpoints** (e.g.
  CoinMarketCap's keyless DEX x402 endpoints). Two fixes to `_call_x402_url`:
  - **Canonical resource**: the signed payment's `resource` now uses the URL the
    server declares in its 402 (`resource.url`), not our request URL with query
    params. Servers like CMC declare the bare path (`…/dex/search`) while we
    request `…?q=BNB`; signing the request URL caused a "resource in payment
    header does not match required resource" rejection (off-chain, no funds lost).
  - **HTTP method**: the flow was POST-only; GET-only resources answered 405 after
    payment. The probe now retries as GET on a 405, the server's method is read
    from the 402's `extensions.bazaar.info.input.method` (default POST), and the
    paid retry is issued with that method (GET → query params + `X-PAYMENT` header).
  Verified live: a $0.01 settle to CMC `dex/search` now returns DEX data. AgentPay's
  own POST tools are unaffected.

## [0.2.6] — 2026-06-11

### Changed
- Base-unavailable diagnostics: when a 402 offers Base but the wallet can't
  settle there, the SDK now says WHY (missing `[base]` extra / venv not
  activated / bad key) — both as a warning at fallback time and inside the
  `PaymentFailed` funding hint. Previously it silently degraded to Stellar
  and surfaced only the Stellar error.

## [0.2.5] — 2026-06-11

### Added
- **`Session.estimate_plan(steps, budget=None)`** — price a multi-tool plan
  before spending anything, via the gateway's new free `POST /v1/plan/estimate`.
  Returns per-step cost, total, fits-budget verdict (defaults to the session's
  remaining budget), and a cheaper same-category alternative per paid step.

### Changed
- `AgentWallet.get_usdc_balance()` now raises `RuntimeError` when Horizon is
  unreachable instead of returning `"0"` — an infra blip no longer silently
  clamps `budget_policy()` spend caps to zero. `"0"` strictly means an
  unfunded account or missing trustline.

### Fixed
- Underfunded errors also trigger the funding hint on Horizon's
  `Resource Missing` (unfunded account); ImportError during Base settlement
  now names the `[base]` extra to install.
- `BASE_AGENT_KEY` env var takes precedence over client-side key minting in
  `quickstart()`.

## [0.2.4] — 2026-06-11

### Added
- **`quickstart()` mints a Base/EVM wallet client-side** (when `eth_account`
  is installed, i.e. `pip install "agentpay-x402[base]"`). The default paid
  chain is Base, so the minted wallet now has a fundable `0x` address from the
  first call instead of dead-ending on a Stellar-only wallet. The secret never
  leaves the machine. New Session attributes: `base_public_key`,
  `base_secret_key` (set only when minted — save it to reuse the wallet).
- `POST /v1/agent/register` accepts `network="both"` and returns a `wallets`
  object with both a Stellar and a Base wallet (gateway-side, for raw-API agents).

### Changed
- Underfunded payment failures (`op_underfunded`, missing trustline, unfunded
  account) now raise `PaymentFailed` with the agent's own fundable address(es)
  in the message, instead of a bare Stellar result code.

## [0.2.3] — 2026-06-01

### Changed
- **Base is now the DEFAULT paid settlement chain; Stellar is the fallback.**
  Previously paid calls picked the *cheapest payable* option (which, with equal
  prices, leaned Stellar). Now both the named-tool path and external x402 URLs
  prefer Base/EIP-3009 (Mode A) when the wallet has a Base key and the 402 offers
  a Base option — the CDP-facilitator path that keeps AgentPay discoverable on
  Bazaar — and fall back to Stellar automatically otherwise.
- The **named-tool paid path now supports Base** (it was Stellar-only). Paid
  AgentPay tools settle gaslessly via EIP-3009 (`AgentPayClient._settle_base`),
  falling back to Stellar on any failure.

### Added
- **`prefer_chain` on `quickstart()` and `Session`** to pin the default chain
  (e.g. `prefer_chain="stellar"`). An explicit chain (per-call `chain=` or session
  `prefer_chain=`) is a hard requirement and raises `PaymentFailed` if unpayable;
  the implicit Base default degrades silently to Stellar.
- `DEFAULT_PAID_CHAIN = "base"` constant in `agentpay/_wallet.py`.

### Unchanged
- Free ($0) tools never settle on-chain and ignore the chain preference (they keep
  flowing through the x402 lifecycle for receipts/analytics).

## [0.2.2] — 2026-05-31

### Added
- **Settlement chain is observable.** `ToolResult.network` is now populated for
  every paid path (AgentPay tools and third-party x402 tools), and each
  `spending_summary()` breakdown row carries `network`. The auto-printed session
  summary shows the chain per call. (One receipt, every chain — in the data.)
- **Explicit chain selection** for external x402 URLs: `session.call(url, chain="base")`
  and a session default `Session(..., prefer_chain="stellar")`.

### Changed
- **Robust payment-option selection.** When a 402 offers multiple networks, the
  SDK normalizes the options and picks the **cheapest payable** by default (or the
  explicitly requested chain). Unpayable/unknown chains now raise a clear
  `PaymentFailed` that lists what the tool offers vs what the wallet can pay,
  instead of a cryptic parse error. AgentPay-native `payment_options` 402s reached
  via URL now return guidance ("call AgentPay tools by name").

## [0.2.1] — 2026-05-31

### Added
- **`ToolResult`** — `session.call()` now returns a dict subclass with
  `.data` (inner tool output), `.cost`, `.tx`, `.network`. Fully backward
  compatible: `r["result"]`, `r["payment"]` still work.
- **Numeric budget accessors** — `remaining_usd()`, `spent_usd()`,
  `tool_cost_usd()` return `Decimal` for safe comparisons; `would_exceed()` now
  accepts str/float/Decimal.

### Fixed
- **Exact float budget caps.** `max_spend=0.10` (float) is coerced through
  `Decimal(str(...))`, so it equals `Decimal("0.10")` exactly (no float drift).
  `"0.10"` and `Decimal("0.10")` continue to work.
- README quickstart no longer shows the broken `AgentWallet(network=...)` (missing
  `secret_key`) example.

## [0.2.0] — 2026-05-30

### Added
- **`quickstart()`** — zero-setup one-liner: registers an agent, mints a wallet,
  and returns a ready budget-capped `Session`. No keys, no funding, no human.
  Free tools work immediately. `quickstart(secret_key=..., base_key=...)` to
  bring your own wallet.
- **`budget_policy()` / `BudgetDecision`** — decide a session cap from a clear
  precedence (explicit → env → interactive → policy → default), clamped to the
  wallet balance, with an approval gate.
- **Base settlement** via off-chain EIP-3009 (gasless, CDP facilitator). Pay
  third-party x402 tools on Base without losing funds on a rejected call.
- **`[base]` optional extra** — `pip install "agentpay-x402[base]"` pulls
  `eth-account` + `x402[evm]`. Core install stays Stellar-only and light.

### Fixed
- Free tools ($0) work without a funded wallet: they flow through the x402
  lifecycle (for receipts/analytics) but skip on-chain settlement.

## [0.1.x]

Initial releases: `AgentWallet`, budget-aware `Session`, Stellar settlement,
`session.call()` for AgentPay tools and external x402 URLs, `discover()`,
`spending_summary()`, faucet wallet.

[0.3.0]: https://pypi.org/project/agentpay-x402/0.3.0/
[0.2.7]: https://pypi.org/project/agentpay-x402/0.2.7/
[0.2.3]: https://pypi.org/project/agentpay-x402/0.2.3/
[0.2.2]: https://pypi.org/project/agentpay-x402/0.2.2/
[0.2.1]: https://pypi.org/project/agentpay-x402/0.2.1/
[0.2.0]: https://pypi.org/project/agentpay-x402/0.2.0/
