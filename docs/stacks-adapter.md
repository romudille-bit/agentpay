# Stacks sBTC Adapter — Design & Build Checklist

Implemented and live on mainnet: the signing library (`agentpay/_stacks_tx.py`,
fixture-validated byte-for-byte against @stacks/transactions), the SDK payment
path (`chain="stacks"` / `Session(prefer_chain="stacks")`), the gateway
settlement adapter (`gateway/stacks.py`, behind `STACKS_ENABLED`), and
USD→sats pricing quoted at 402-issuance and stored on the challenge. History
and per-release detail are in `CHANGELOG.md`; mainnet operation is in
`stacks-mainnet.md`.

## Wire contract

**402 offer** — the gateway advertises Stacks in AgentPay's native 402 body as
`payment_options.stacks` (amount_sats/amount_usdc computed at 402-issuance):

```json
{
  "payment_options": { "stacks": {
      "scheme": "exact",
      "network": "stacks:2147483648",     // CAIP-2; stacks:1 on mainnet
      "amount_sats": 1030,                 // what gets signed (sBTC, sats)
      "amount_usdc": "0.001",              // USD-at-quote — budget/cap math
      "btc_usd_rate": "97000",             // rate this quote used
      "pay_to": "ST…",                     // gateway c32 address
      "fee_microstx": 500                  // suggested STX network fee (optional)
  }}
}
```

The SDK bounds `amount_usdc` by the session cap BEFORE signing (fail-closed on
an unparseable amount) and refuses a CAIP-2 network that doesn't match the
wallet's network.

**payment retry** — lowercase `payment-signature` header (third dialect) +
`x-agent-address` (c32). Header value = base64 JSON:

```json
{
  "x402Version": 2,
  "scheme": "exact",
  "network": "stacks:2147483648",
  "payment_id": "<uuid>",                       // challenge lookup key; the tx's
                                                 // memo must bind to the same id
  "payload": {
    "signedTransaction": "<hex SIP-005 tx>",   // complete, unbroadcast
    "txid": "<hex sha512/256>"                  // pre-broadcast, replay key
  },
  "accepted": { "scheme": "exact", "network": "…", "amount": "<sats>",
                "asset": "sbtc", "payTo": "ST…", "resource": "<url>",
                "mimeType": "application/json" }
}
```

The gateway recomputes `txid_of(signedTransaction)` and IGNORES the
client-supplied txid for the replay consume (never trust the header's copy).
It resolves the pending challenge by `payment_id`, then requires the memo
INSIDE the signed tx to match it (prefix rule — the (buff 34) memo truncates
36-char UUIDs to 34); the challenge fixes the expected sats. The payment_id
is consumed (record_payment_id, fail-closed) BEFORE broadcast so a second tx
can never double-fulfil one challenge.

**settle responses the SDK understands** (on non-200, JSON body):
- `payment_status: "rejected"` + `error_reason` matching `/bad|conflicting|
  stale.{0,3}nonce/i` → the SDK zeroes the leg and re-signs ONCE with a fresh
  chain nonce. Return `rejected` ONLY when the node refused the broadcast
  (nothing in any mempool) — never for an ambiguous timeout.
- `payment_status: "rejected"` (non-nonce reason, e.g.
  `abort_by_post_condition`) → SDK zeroes the leg, raises PaymentFailed.
- `payment_status: "refund_pending"/"refund_disabled"` → standard RefundPending
  contract, spend stays recorded.
- anything else non-200 → SDK keeps the spend recorded as
  `uncertain_settlement` and treats the nonce as consumed.

## Why this document exists

The gateway/SDK review before 0.3.0 surfaced the defect classes a third
settlement path would otherwise copy. This document distills them into a
12-point checklist for anyone touching payment code in this adapter.
Reference implementations live in `agentpay/_wallet.py` and `gateway/base.py`.

## The third settlement model

| | Stellar | Base (Mode A) | **Stacks** |
|---|---|---|---|
| Who broadcasts | agent | CDP facilitator | **gateway** (via facilitator /settle, or direct Hiro) |
| Payment artifact | on-chain tx (done) | off-chain EIP-3009 auth | **fully signed, unbroadcast tx** |
| Gateway's job | verify | verify+settle via CDP | **broadcast + confirm + recover** |
| Challenge binding | memo = payment_id | tx hash consumption | **memo arg of `sbtc-token::transfer` = payment_id** |
| Replay key | tx hash (post-hoc) | tx hash | **txid — deterministic before broadcast** |
| Facilitator dies | n/a | Mode A unavailable | **degrade to direct `POST /v2/transactions` on Hiro** |

Key consequence: every failure mode between broadcast and confirmation lands
on the gateway, so settle-timeout → poll-by-txid → `ok_recovered` is part of
the design rather than a later patch.

## Header dialect (the third one — keep them straight)

- Stellar: `X-Payment: tx_hash=…,from=…,id=…` (+ legacy `x-payment-required`)
- Base x402 v2: `PAYMENT-SIGNATURE` / `PAYMENT-REQUIRED` (uppercase)
- **Stacks x402 v2: lowercase `payment-required` / `payment-signature` /
  `payment-response`, CAIP-2 network `stacks:1` (mainnet) /
  `stacks:2147483648` (testnet).** Parse case-insensitively; emit the dialect
  each rail expects.

## The 12-point build checklist

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
    as `min(remaining + own_hold, quote × 1.05)` after the hold lands, under
    one lock. Reference: `_wallet.py::_cap_excluding_hold`.
12. **Book spend and release the hold in one locked section** —
    absorb-before-release; two lock acquisitions re-open the TOCTOU.
    Reference: `_wallet.py::_absorb_and_release`.

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
- Pricing — live BTC/USD (CoinGecko, cached, fixed fallback);
  the quote (sats + rate) is stored on the challenge — in memory and in the
  `pending_challenges` row (`stacks_sats`, `stacks_rate`) — and read back at
  settle, so it survives a gateway restart; rate + sats on the 402 option and
  the settle receipt. USDCx deferred (see below).
- Testnet: `TESTNET_PAID_TOOLS` gives the testnet registry at least one
  priced tool, so the capped session, real payment, and over-cap rejection
  are all demonstrable there.

## Known limitations & dependencies

- The Stacks x402 facilitator ecosystem is young. Posture: **facilitator is
  convenience, not a hard dependency** — the payment artifact is a complete
  signed tx, so settlement degrades gracefully to direct Hiro broadcast +
  confirmation polling.
- Agents need STX for network fees, unless a sponsored-relay path co-signs;
  the sponsored-transaction flag is supported in the wire format from day
  one, relay integration itself comes later.
- sBTC is BTC-denominated: dollar prices require FX at issuance.
- **USDCx deferred.** USDCx (Circle xReserve-backed USDC on Stacks) would
  sidestep FX, but the grant milestones require the sBTC path and x402
  dollar-stablecoin demand on Stacks is negligible today. Additive, not a
  blocker; the FX path keeps the sBTC quote dollar-accurate meanwhile.
- The issuance quote lives on the challenge, so a settle on another worker
  or after a restart reads it from `pending_challenges`. The row is written
  only when the 402 request identifies a payer (`agent_address` in the body
  or `x-agent-address`), which the SDK always sends; an anonymous 402 keeps
  the in-memory challenge only. A challenge issued without a Stacks option
  (or without a row) falls back to a re-quote, where the verify tolerance
  absorbs small drift.
- No mature Python Stacks signing lib exists — `_stacks_tx.py` is a minimal,
  spec-documented implementation (SIP-005/SIP-010), fixture-validated against
  stacks.js. secp256k1 primitives come from the existing dependency tree.
