# Stacks sBTC on mainnet

How the production gateway (`agentpay.tools`) offers and settles sBTC
payments, and how an agent pays. Testnet setup and the M1 demo are in
[`stacks-m1.md`](stacks-m1.md); the wire contract and design are in
[`stacks-adapter.md`](stacks-adapter.md).

## Gateway configuration

Set on the production Railway service. Everything else derives from
`STACKS_NETWORK`.

```
STACKS_ENABLED=true
STACKS_NETWORK=mainnet
STACKS_GATEWAY_ADDRESS=SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C
```

The payee address only receives. The gateway broadcasts transactions the
agent signed, it never signs one itself, so the address holds no STX and no
key is deployed anywhere. Mainnet values the gateway derives: Hiro API
`https://api.hiro.so`, sBTC contract
`SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token`, CAIP-2 network
`stacks:1`.

Optional: `STACKS_FACILITATOR_URL` (a Stacks x402 facilitator's `/settle`;
blank means the gateway broadcasts to Hiro directly and that is the
supported path), `STACKS_FIXED_BTC_USD` (rate fallback), the fee knobs
below. Do **not** set `TESTNET_PAID_TOOLS` on mainnet — the registry prices
apply.

Before deploying a gateway with Stacks enabled, apply
`db/migrations/pending_challenges_stacks_quote.sql` (two nullable columns
that keep the issued quote on the challenge row).

## What a mainnet 402 looks like

Every priced tool's 402 gains a `payment_options.stacks` block:

```json
{
  "scheme": "exact",
  "network": "stacks:1",
  "amount_sats": 13,
  "amount_usdc": "0.01",
  "btc_usd_rate": "77134",
  "pay_to": "SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C",
  "fee_microstx": 3000,
  "asset": "sbtc"
}
```

`amount_sats` is the USD price converted at issuance (ceil to the sat) and is
stored on the challenge, so the settle verifies against that quote even if
BTC moves or the gateway restarts in between. `fee_microstx` is the STX fee
the gateway suggests: the medium tier of Hiro's `/v2/fees/transaction` for
an sBTC transfer, never below `STACKS_SUGGESTED_FEE_MICROSTX` (3,000) and
never above `STACKS_FEE_CAP_MICROSTX` (20,000). The fast tier was tried
first and swung between 759 and 100,000+ µSTX within a day. The SDK clamps
whatever a gateway suggests to 0.05 STX (`STACKS_MAX_FEE_MICROSTX`,
lower-only).

## Paying from the SDK

```python
from agentpay import AgentWallet, Session

wallet = AgentWallet(network="mainnet", stacks_key=os.environ["STACKS_AGENT_KEY"])
with Session(wallet, max_spend="0.05", prefer_chain="stacks") as s:
    result = s.call("pre_trade_check", {"symbol": "BTC"})
```

`stacks_key` is the payer's private key — 66 hex ending in `01`
(compressed, what wallets export) or 64 hex (uncompressed); both derive
and sign correctly. The payer holds sBTC to spend and a little STX for
fees. The Stellar secret is optional for a Stacks-only payer.

Leather exports a 24-word Secret Key rather than a raw key.
`tools/stacks_derive_key.py` turns it into the 66-hex form on the same path
Leather uses (`m/44'/5757'/0'/0/<account>`); the words go into a hidden
prompt and the key goes straight into the variable:

```bash
export STACKS_AGENT_KEY=$(python tools/stacks_derive_key.py --address SP<payer> --print-key)
```

The cap binds before signing: the SDK refuses to sign an `amount_sats` the
USD cap does not bound at a floor BTC/USD rate, or one inconsistent with the
quoted rate, and `max_per_tool` / `allowed_tools` apply as on any rail.

## When the gateway cannot confirm in time

The gateway broadcasts and polls Hiro for confirmation inside the request,
under one wall-clock bound (`STACKS_SETTLE_DEADLINE_S`, 75 s — below the
edge's 100 s cut, so the reply always has a body). If the transaction is
still pending when the window closes, the call ends with
`SettlementUncertain`: the spend is recorded, the transaction is live, and
the gateway keeps a `payment_logs` row for it in state `uncertain`.

Redeem it:

```python
from agentpay import SettlementUncertain

try:
    result = s.call("pre_trade_check", {"symbol": "BTC"})
except SettlementUncertain as e:
    result = s.redeem(e, wait_s=600)   # wait for confirmation, re-present the same payment
```

`redeem` polls the transaction on Hiro and, once it is `success`, re-sends
the identical `payment-signature` header. The gateway recognises the txid
(recomputed from the bytes, never taken from the header), flips the row
`uncertain → verified` in a single compare-and-set, and delivers once. A
second redemption of the same payment is refused. If the transaction
aborted on-chain the row closes as `rejected` and `redeem` raises
`PaymentFailed`; nothing moved.

The SDK does not take a gateway's `rejected` on faith: before it zeroes a
leg it asks Hiro for the txid, and unless the chain shows the transaction
unknown or aborted, the spend stays recorded and the call ends
`SettlementUncertain` with a redeem context. A gateway that broadcast and
then claimed rejection cannot produce unrecorded spend.

The uncertain reply is HTTP 503 with a JSON body (`payment_status:
"uncertain"`), not 502: Cloudflare replaces an origin 502/504 with its own
HTML page, which is what `agentpay.tools` sits behind. If the redeem itself
hits an edge error page, `redeem` raises `SettlementUncertain` again with
the same context rather than giving up.

If the process that signed is gone, the txid is enough — the signed bytes
and the challenge id (the memo) are read back from Hiro:

```python
result = s.redeem_txid("30689b5e…", "pre_trade_check", {"symbol": "BTC"}, wait_s=600)
```

or, from the demo, `STACKS_REDEEM_TXID=<txid> STACKS_NETWORK=mainnet python
examples/stacks_m1_demo.py`.

## Receipts and the ledger

Each settled Stacks call returns `payment.tx_hash` (the txid) and
`payment.network` (`stacks-mainnet`). Receipt legs with a Stacks txid are
chain-verified by the ledger's background verifier: the txid must be a
confirmed `sbtc-token::transfer` to the gateway's payee address, from the
run's wallet when known. Such legs render as **chain-verified** on
[agentpay.tools/ledger](https://agentpay.tools/ledger) with an
`explorer.hiro.so` link. The ledger lists only the wallets in
`LEDGER_FLAGSHIP_ADDRESSES`; add the Stacks payer (`SP…`) there for its
receipts to appear.

## The pilot agent

The flagship analyst — the daily cron that buys `pre_trade_check` verdicts
under a `$0.25` cap and publishes its reasoning to the ledger — runs on the
Stacks rail when its Railway service carries `FLAGSHIP_STACKS_KEY` (and,
optionally, `FLAGSHIP_RAIL=stacks|base|alternate`). Every gateway-paid leg
of such a run is an sBTC transfer from the pilot wallet, chain-verified on
`/ledger` like any other Stacks receipt; the reasoning row names the Stacks
address as the payer. `agents/analyst/README.md` covers funding and the
uncertain-settle path (the run waits for the confirmation and redeems the
same signed transaction rather than dropping the verdict).

## Reproducing

```bash
export STACKS_AGENT_KEY=<funded mainnet payer key>
STACKS_NETWORK=mainnet python examples/stacks_m1_demo.py
```

The script pays `pre_trade_check` once under a $0.05 session cap (redeeming
if the settle was uncertain), then shows the same tool refused client-side
under a per-tool cap below its price, with nothing signed. Each priced call
is $0.01; at current rates that is a few dozen sats plus a fee of a few
thousand µSTX.

## Known limitations

- Standard (payer-pays-fee) transactions only; sponsored transactions are
  refused at verification.
- One in-flight Stacks payment per wallet: nonces are sequential and the SDK
  serialises signing.
- The settle waits for confirmation inside the HTTP request, so a slow block
  surfaces as `SettlementUncertain` rather than a 200; `redeem` is the
  recovery path.
