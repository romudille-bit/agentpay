# Stacks sBTC Adapter — Design & Build Checklist

**Status:** scaffold. Skeletons: `agentpay/_stacks_tx.py` (signing lib),
`gateway/stacks.py` (settlement adapter). Nothing imports them yet — zero
runtime impact until wired in.

## Why this document exists

The 2026-07 gateway/SDK code review (fixes shipped in `agentpay-x402` 0.3.0
and 0.3.1 — see CHANGELOG) surfaced the exact defect classes a third
settlement path would otherwise copy. This document distills them into a
12-point checklist to be read **before writing any payment code in this
adapter**. Reference (fixed) implementations live in `agentpay/_wallet.py`
and `gateway/base.py`.

## The third settlement model

| | Stellar | Base (Mode A) | **Stacks** |
|---|---|---|---|
| Who broadcasts | agent | CDP facilitator | **gateway** (via facilitator /settle, or direct Hiro) |
| Payment artifact | on-chain tx (done) | off-chain EIP-3009 auth | **fully signed, unbroadcast tx** |
| Gateway's job | verify | verify+settle via CDP | **broadcast + confirm + recover** |
| Challenge binding | memo = payment_id | tx hash consumption | **memo arg of `sbtc-token::transfer` = payment_id** |
| Replay key | tx hash (post-hoc) | tx hash | **txid — deterministic PRE-broadcast** |
| Facilitator dies | n/a | Mode A unavailable | **degrade to direct `POST /v2/transactions` on Hiro** |

Key consequence: every failure mode between broadcast and confirmation lands
on the gateway. The settle-timeout → poll-by-txid → `ok_recovered` pattern
(learned in production on the Base path) is designed in from day one, not
patched in later.

## Header dialect (the third one — keep them straight)

- Stellar: `X-Payment: tx_hash=…,from=…,id=…` (+ legacy `x-payment-required`)
- Base x402 v2: `PAYMENT-SIGNATURE` / `PAYMENT-REQUIRED` (uppercase)
- **Stacks x402 v2: lowercase `payment-required` / `payment-signature` /
  `payment-response`, CAIP-2 network `stacks:1` (mainnet) /
  `stacks:2147483648` (testnet).** Parse case-insensitively; emit the dialect
  each rail expects.

## The 12-point build checklist (do NOT copy the pre-fix patterns)

Each item is anchored in the skeletons as `[CHECKLIST #N]` at the exact place
it must be enforced.

1. Bound the signed amount by remaining budget; reject a 402 amount above the
   quote **before signing**.
2. Record spend at signature-transmission time, not at HTTP 200.
3. Never fall back to another chain while a signed Stacks tx is live; treat
   transmitted signatures as pending spend.
4. Route the Stacks path through `allowed_tools` / `max_per_tool`.
5. Bind the payment to the challenge — `payment_id` in the SIP-010 transfer
   memo arg — never amount alone.
6. Fail **closed** on the replay consume.
7. Clamp the signature validity window client-side before signing (≤600s).
8. Constant key-parse error strings; wrap key loading so secrets never appear
   in exception text.
9. Update the wallet-level spend counter for sign-don't-broadcast settles.
10. `await` the receipt insert before the terminal state PATCH.
11. **Cap math must exclude the call's own hold**: compute the client ceiling
    as `min(remaining + own_hold, quote × 1.05)` AFTER the hold lands, under
    one lock. Reference: `_wallet.py::_cap_excluding_hold` (0.3.1).
12. **Book spend and release the hold in ONE locked section** —
    absorb-before-release; two lock acquisitions re-open the TOCTOU.
    Reference: `_wallet.py::_absorb_and_release` (0.3.1).

## Module map

- `agentpay/_stacks_tx.py` — pure signing lib: c32check, Clarity
  serialization, SIP-005 wire format, presign-sighash, mandatory
  post-conditions, pre-broadcast txid, sponsored-flag support. No I/O, no
  gateway knowledge. Fixture-tested against stacks.js output.
- SDK integration — `chain="stacks"` in `_client.py`/`_wallet.py`:
  sign-don't-broadcast semantics, client-side sequential nonce serialization
  (one in-flight signed tx per wallet), checklist items 1-4, 7, 9, 11-12.
- `gateway/stacks.py` — settlement adapter: facilitator verify/settle,
  direct-Hiro fallback, `ok_recovered` poll path, atomic pre-settle txid
  consume, Clarity contract-call decode for verification.
- Pricing — USD→sats FX at 402-issuance + overpay tolerance; USDCx as the
  dollar-priced option. Rate + quoted amount + validity window recorded in
  payment_logs for receipt auditability.
- Testnet: at least one nonzero-priced tool on the testnet registry (the
  free-funnel pricing left testnet with no payable tool), so the capped
  session, real payment, and over-cap rejection are all demonstrable.

## Known limitations & dependencies

- The Stacks x402 facilitator ecosystem is young. Posture: **facilitator is
  convenience, not a hard dependency** — the payment artifact is a complete
  signed tx, so settlement degrades gracefully to direct Hiro broadcast +
  confirmation polling.
- Agents need STX for network fees, unless a sponsored-relay path co-signs;
  the sponsored-transaction flag is supported in the wire format from day
  one, relay integration itself comes later.
- sBTC is BTC-denominated: dollar prices require FX at issuance; USDCx
  remains the dollar-priced alternative.
- No mature Python Stacks signing lib exists — `_stacks_tx.py` is a minimal,
  spec-documented implementation (SIP-005/SIP-010), fixture-validated against
  stacks.js. secp256k1 primitives come from the existing dependency tree.
