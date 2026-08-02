# Stacks sBTC settlement — developer guide (M1)

This is the developer reference for AgentPay's Stacks/sBTC settlement adapter as
shipped for milestone M1 of the Stacks Endowment grant. It covers how to set up a
payer and a testnet gateway, how the payment flows, the known limitations of the
testnet rail, and the external dependencies the rail leans on. For a runnable
proof of the two M1 acceptance criteria, see `examples/stacks_m1_demo.py` and its
README.

## What the adapter does

The adapter adds a Stacks rail to AgentPay's x402 micropayment client. When a tool
behind the gateway returns an HTTP `402` challenge that offers a Stacks option, the
SDK builds and **signs** a SIP-010 `sbtc-token::transfer` from the payer to the
gateway's Stacks address, then hands the signed transaction to the gateway, which
**broadcasts** it to the Stacks node. The SDK signs but never broadcasts itself —
the payment artifact that leaves the client is a complete, independently
broadcastable transaction. That property is the backbone of the graceful-degradation
posture described under Known limitations below.

Chain identifiers follow CAIP-2: `stacks:1` for mainnet and `stacks:2147483648`
for testnet. Budget enforcement (session cap, per-tool cap, tool allowlist, rate
limit) happens client-side, *before* anything is signed, so a call that violates a
cap costs nothing and moves no value.

## Setup

### 1. Install

```bash
pip install agentpay
```

The public entry point is `agentpay.Session`, wrapping an `AgentWallet`.

### 2. Fund a testnet payer

The payer wallet needs two testnet assets:

- **testnet sBTC** — the value actually transferred to the gateway, and
- **testnet STX** — used only to pay the transaction fee.

Fund both from the Hiro faucet at `https://platform.hiro.so/faucet`. If the web
faucet is flaky, the STX portion can be requested directly from the API faucet:

```bash
curl -X POST "https://api.testnet.hiro.so/extended/v1/faucets/stx?address=<ST…>"
```

Use the payer's `ST…` (c32-encoded testnet) address, not its private key, when
requesting from a faucet.

### 3. Run against the testnet gateway

The payer's Stacks private key stays in your environment and is never committed:

```bash
export STACKS_AGENT_KEY=<funded payer Stacks private key>
python examples/stacks_m1_demo.py
```

Minimal programmatic usage:

```python
from agentpay import AgentWallet, Session, SettlementUncertain, PaymentFailed

wallet = AgentWallet(secret_key=<stellar secret>, network="testnet",
                     stacks_key=os.environ["STACKS_AGENT_KEY"])
assert wallet.stacks_address, wallet.stacks_disabled_reason

session = Session(
    wallet=wallet,
    gateway_url="https://gateway-testnet-production.up.railway.app",
    max_spend="0.05",          # session cap, USD
    prefer_chain="stacks",
)

try:
    result = session.call("token_price", {"symbol": "BTC"})
except SettlementUncertain as e:
    # Expected clean outcome on testnet — broadcast, confirming asynchronously.
    print("broadcast; confirming on-chain:", e.tx_hash, e.network)
except PaymentFailed as e:
    # Nothing settled.
    print("payment failed:", e)
```

`AgentWallet` currently requires a Stellar secret for construction; it is unused on
the Stacks pay path (a throwaway keypair is fine for a Stacks-only payer).

### 4. Configure a gateway (server side)

To stand up your own testnet gateway with the Stacks rail enabled and a
nonzero-priced tool, set:

```
STACKS_ENABLED=true
STACKS_NETWORK=testnet
STACKS_GATEWAY_ADDRESS=<ST…>            # the gateway's testnet payee address
TESTNET_PAID_TOOLS=token_price:0.01     # price a demo tool at $0.01
```

`TESTNET_PAID_TOOLS` is an env-driven price override, so a normally-free tool
(`token_price`) can be priced for the demo without a registry change. With it set,
the gateway issues a `402` for that tool and quotes the USD price to sats at
issuance time using a live BTC/USD rate, pinned for the life of that one payment.

## How a payment flows

1. The client calls a paid tool; the gateway responds `402` with a Stacks payment
   option, USD price quoted to **sats** at issuance (rate pinned per payment).
2. The client checks every budget cap first. If any cap is exceeded, it raises
   `BudgetExceeded` here — before signing — and no value moves.
3. The SDK builds and **signs** the `sbtc-token::transfer` (payer → gateway) but
   does **not** broadcast it.
4. The gateway receives the signed transaction and **broadcasts** it — directly to
   a Hiro node, or via a facilitator when one is configured.
5. Because Stacks testnet blocks take a few minutes, confirmation is
   **asynchronous**. The gateway cannot hold an HTTP connection open that long, so
   the SDK surfaces `SettlementUncertain` carrying the `tx_hash`. **This is the
   expected clean outcome, not an error** — the transaction is on-chain and
   confirms shortly after.

### Handling `SettlementUncertain`

`SettlementUncertain` subclasses `PaymentFailed`, so existing `except PaymentFailed`
handlers keep working. It carries `.tx_hash` and `.network`. The spend is already
recorded, so **do not retry** — a retry would double-pay. Verify `.tx_hash` on the
explorer instead:

```
https://explorer.hiro.so/txid/<tx_hash>?chain=testnet
```

The client-side settle read timeout is set to 180s, comfortably above the gateway's
server-side confirmation-poll window, so the client receives the reply (and the
txid) rather than blind-timing-out and losing the id.

## Known limitations

**Testnet confirmation is slow and asynchronous.** Stacks testnet blocks take
minutes. `SettlementUncertain` with a txid is the normal successful path on testnet,
not a failure; callers should treat a returned txid as proof of payment and verify
on-chain rather than retrying.

**The facilitator is a convenience, not a hard dependency.** The payment artifact
the SDK produces is a fully signed transaction. If a facilitator is configured, the
gateway can route through it; if the facilitator has an outage or is not configured,
settlement **degrades gracefully to direct Hiro broadcast** of the same signed
transaction. A young facilitator being down therefore does not fail the payment path
or the milestone — it removes a convenience layer, not the settlement itself.

**STX is required for fees.** The payer must hold testnet STX to cover the
transaction fee, in addition to the sBTC being transferred. Where holding STX on the
payer is undesirable, a sponsored-relay (fee-sponsorship) path can cover the fee so
the payer needs only sBTC; see Dependencies.

**Stellar secret required for wallet construction.** `AgentWallet` requires a
Stellar secret to construct even for Stacks-only payers. It is unused on the Stacks
pay path; a random keypair suffices.

## Dependencies

**Stacks x402 facilitator(s).** Optional convenience layer for broadcast/settlement.
Community facilitators in this ecosystem include tony1908's and aibtcdev's. Because
the SDK emits a fully signed transaction, the gateway falls back to direct Hiro
broadcast when no facilitator is available (see Known limitations).

**Hiro API.** Used for node broadcast, transaction/confirmation lookups, and the
testnet faucet (`https://platform.hiro.so/faucet`, and the STX API faucet at
`https://api.testnet.hiro.so/extended/v1/faucets/stx`). Explorer links resolve at
`https://explorer.hiro.so`.

**STX for fees, with a sponsored-relay escape hatch.** Every Stacks transaction
needs an STX fee. The default path funds the payer with testnet STX; the
sponsored-relay path lets a sponsor cover the fee so a payer holding only sBTC can
still transact.

## Reproduce it yourself

A clean-room run of the two acceptance criteria against the live testnet gateway,
in a fresh virtualenv (spinner frames elided):

```
$ python3 -m venv venv && . venv/bin/activate
$ pip install -r requirements.txt
$ export STACKS_AGENT_KEY=<funded payer Stacks private key>
$ python examples/stacks_m1_demo.py

====================================================================
1) BUDGET-CAPPED SESSION  ->  sBTC PAYMENT ON STACKS TESTNET
====================================================================
payer (Stacks testnet): ST1EGAZTB1DK5ED2SM9Q716SJCFDE08WH2AZNPA66
session cap: $0.05   paying token_price({'symbol': 'BTC'}) in sBTC ...

  ✓ sBTC PAYMENT BROADCAST — confirming on-chain
  TX     : 90e8a7c56a0e5b3752f7bca449c93549b2be878409497204a8345d479cd8e86e
  NETWORK: stacks
  RECEIPT: {'calls': 1, 'spent': '$0.01', 'remaining': '$0.04', 'budget': '$0.05', ...}
  verify : https://explorer.hiro.so/txid/90e8a7c5…?chain=testnet

====================================================================
2) PER-TOOL CAP BELOW PRICE  ->  REJECTED BEFORE ANY PAYMENT
====================================================================
session cap $0.05, per-tool cap $0.005 (< $0.01 price)   calling token_price ...
  ✓ rejected client-side — BudgetExceeded: Per-tool cap for 'token_price':
     this call ($0.01) would bring spend to $0.01, over the $0.005 cap
  RECEIPT: {'calls': 0, 'spent': '$0', ...}  (nothing spent)
```

Confirm the payment independently on the Hiro API — no repo access required:

```
$ curl -s https://api.testnet.hiro.so/extended/v1/tx/0x<txid> | \
    python3 -c "import sys,json; d=json.load(sys.stdin); \
    print(d['tx_status'], d['contract_call']['contract_id'], d['contract_call']['function_name'])"
success ST1F7QA2MDF17S807EPA36TSS8AMEFY4KA9TVGWXT.sbtc-token transfer
```

## Verified on-chain

Confirmed budget-capped sBTC settlements on Stacks testnet — both
`sbtc-token::transfer`, payer → gateway, status `success`:

- [`0x63e9e9b8…`](https://explorer.hiro.so/txid/0x63e9e9b8e14b742173e87a235b0e2f4657094a5520a8f928d0d01d7c1e7d7287?chain=testnet)
  — first confirmed settlement.
- [`0x90e8a7c5…`](https://explorer.hiro.so/txid/0x90e8a7c56a0e5b3752f7bca449c93549b2be878409497204a8345d479cd8e86e?chain=testnet)
  — the reproduction run above (block 4054292).
