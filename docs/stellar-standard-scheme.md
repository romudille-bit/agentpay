# Standard x402 on Stellar

The gateway accepts the x402 `exact` scheme on `stellar:pubnet` / `stellar:testnet`
as specified in `specs/schemes/exact/scheme_exact_stellar.md` of the x402 repo.
This is a second Stellar path next to the classic payment+memo rail the AgentPay
SDK uses; the spec supports Soroban (SEP-41) tokens only, so the classic rail can
never be the standard one and stays as it is.

## Flow

1. `POST /tools/{name}/call` without payment → 402. For a priced tool the
   `accepts[]` (body and `PAYMENT-REQUIRED` header) ends with a `stellar:*`
   entry: `exact`, the USDC Stellar Asset Contract, `payTo` = the gateway's
   `G…` account, amount in 7-decimal units, `extra.areFeesSponsored: true`.
2. A stock `@x402/stellar` client builds a USDC `transfer(from, payTo, amount)`
   invocation, signs the auth entries (not the transaction) and retries with
   `PAYMENT-SIGNATURE` carrying `payload.transaction` (base64 XDR).
3. The gateway routes the payload on `accepted.network` (`gateway/stellar_x402.py`)
   and POSTs it with its own requirements to the facilitator's `/verify`, then
   reserves the session hold against the verified payer, then `/settle`.
   The facilitator rebuilds the transaction as source, pays the fee and
   submits it; the gateway signs nothing.
4. The settled transaction hash is consumed (`replay_tx_hashes`, network label
   `stellar-<net>`) and the tool runs. Receipts and `/ledger` carry the hash.

## Configuration

| Variable | Meaning |
|---|---|
| `STELLAR_STANDARD_CLIENTS` | advertise and accept the standard entry (default off) |
| `STELLAR_FACILITATOR_API_KEY` | Bearer key for the facilitator, one per network — `GET https://channels.openzeppelin.com/gen` (pubnet) or `/testnet/gen` |
| `STELLAR_FACILITATOR_URL` / `STELLAR_FACILITATOR_TESTNET_URL` | facilitator base URLs (OpenZeppelin "Built on Stellar" defaults) |
| `STELLAR_FACILITATOR_TIMEOUT_S` | HTTP timeout per facilitator call (settlement polls confirmation) |
| `STELLAR_USDC_SAC` | override of the USDC contract id (defaults match `@x402/stellar`) |
| `STELLAR_MAX_TIMEOUT_S` | `maxTimeoutSeconds` in the entry; bounds the auth-entry expiry the facilitator enforces |

`GATEWAY_PUBLIC_KEY` is the `payTo`; it needs a USDC trustline on the network.

## Failure contract

| Response | Meaning for the client |
|---|---|
| 402 `payment_status: rejected` | nothing moved; fix the payload or get a fresh 402 |
| 503 `payment_status: rejected` | facilitator unreachable before anything was sent to chain; retry later with the same payload |
| 503 `payment_status: uncertain` | the facilitator may have submitted the transfer; do **not** sign a new payment, retry with the same `PAYMENT-SIGNATURE` |

Known limit: an uncertain settle is not yet reconciled from chain on the retry
(the Base path does this); a replayed payload whose auth entry was already
used is rejected by the facilitator, so the transfer can settle once at most.
Free tools never get a `stellar:*` entry — the client refuses a zero amount.
The 85% developer split does not run on this path yet (same gap as Base and
Stacks); every tool pays out to the gateway account today.
