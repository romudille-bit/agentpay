# Changelog

All notable changes to **agentpay-x402** (the `agentpay` Python SDK).
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); this
project uses [Semantic Versioning](https://semver.org/).

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

[0.2.3]: https://pypi.org/project/agentpay-x402/0.2.3/
[0.2.2]: https://pypi.org/project/agentpay-x402/0.2.2/
[0.2.1]: https://pypi.org/project/agentpay-x402/0.2.1/
[0.2.0]: https://pypi.org/project/agentpay-x402/0.2.0/
