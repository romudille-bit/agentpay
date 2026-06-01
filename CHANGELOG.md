# Changelog

All notable changes to **agentpay-x402** (the `agentpay` Python SDK).
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); this
project uses [Semantic Versioning](https://semver.org/).

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

[0.2.2]: https://pypi.org/project/agentpay-x402/0.2.2/
[0.2.1]: https://pypi.org/project/agentpay-x402/0.2.1/
[0.2.0]: https://pypi.org/project/agentpay-x402/0.2.0/
