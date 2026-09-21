# Pay an AI agent's tool call in sBTC — a ten-minute walkthrough

For Stacks builders and reviewers. By the end you will have made one
budget-capped sBTC payment on mainnet from Python, watched the SDK refuse a
payment the rules do not allow before anything was signed, and verified the
receipt two independent ways. Every output below is real; the transaction
ids resolve on Hiro explorer.

The reference for each piece is [`stacks-mainnet.md`](stacks-mainnet.md);
this page is the path through it.

## What you need

- Python 3.10 or newer.
- A fresh Stacks mainnet wallet, holding a few hundred sats of sBTC and about
  0.1 STX for fees. Each paid call below is $0.01 (13–15 sats at current
  rates) plus a fee of a few thousand µSTX. Nothing else: no account, no API
  key, no signup.

We recommend the use of a new wallet, not your everyday one. You are about to
put a private key in an environment variable and hand it to code you have not
audited — ours included. With a few hundred sats in it and nothing else, that
costs you nothing to get wrong.

<details>
<summary>Using Leather for the throwaway wallet</summary>

Leather exports a 24-word Secret Key rather than a raw key.
[`tools/stacks_derive_key.py`](../tools/stacks_derive_key.py) derives the key
on the same path Leather uses, without the words touching your shell history
(the address picks the right account):

```bash
export STACKS_AGENT_KEY=$(python tools/stacks_derive_key.py --address SP<your address> --print-key)
```

The words you type here derive the key it prints, so point it at the wallet
you made for this rather than one holding anything else.
</details>

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install "agentpay-x402[stacks]"
```

```
Successfully installed agentpay-x402-0.5.1 … eth-keys-0.8.0 … stellar-sdk-16.1.0 …
```

The `[stacks]` extra is the secp256k1 signer. The SDK builds and signs the
sBTC transfer itself; it never broadcasts. That is the whole trust model in
one line: the only thing that leaves your machine is a fully signed
transaction with an exact post-condition, and whoever broadcasts it cannot
change what it does.

## 2. See the price before paying anything

Every priced tool answers an unauthenticated request with HTTP 402 and the
payment options. No SDK needed for this step:

```bash
curl -s -X POST https://agentpay.tools/tools/pre_trade_check/call \
  -H 'content-type: application/json' \
  -d '{"parameters": {"symbol": "BTC"}, "agent_address": "<your address or any identifier>"}' \
  | python -m json.tool
```

That is the envelope the tool pages on `agentpay.tools` show: the tool's
arguments under `parameters`, plus an optional `agent_address` that labels
the call on the receipt (the SDK sends it as the `x-agent-address` header
and, on Stacks, sets it to the payer's c32 address). A bare body
(`{"symbol": "BTC"}`) is accepted too and treated as `parameters`; the
reply is the same either way.

The `stacks` option (captured 2026-09-13):

```json
"stacks": {
  "scheme": "exact",
  "network": "stacks:1",
  "amount_sats": 14,
  "amount_usdc": "0.01",
  "btc_usd_rate": "76752",
  "pay_to": "SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C",
  "fee_microstx": 3000,
  "asset": "sbtc",
  "header": "payment-signature: <base64(StacksPaymentPayload JSON)>"
}
```

`amount_sats` is the USD price converted at issuance and stored on the
challenge, so the settle verifies against this exact quote even if BTC
moves before you pay. `pay_to` is the gateway's receive-only address; it
holds no key anywhere.

`fee_microstx` is quoted fresh on every challenge from Hiro's current
medium tier, so unlike the sats it is not fixed — two calls a minute apart
can carry different fees. The SDK signs the fee the 402 quotes and only
clamps it downwards against its own ceiling; it never re-estimates. The
3,000 above is one capture, not a constant.

## 3. Pay once, under a cap

```python
import os
from agentpay import quickstart

s = quickstart(stacks_key=os.environ["STACKS_AGENT_KEY"], prefer_chain="stacks", max_spend="0.05")
r = s.call("pre_trade_check", {"symbol": "BTC"})
print(r.data["verdict"], r.tx)
print(s.spending_summary())
```

Or the runnable version, which also demonstrates the over-cap refusal
(step 5) and the uncertain-settle recovery (step 6):

```bash
STACKS_NETWORK=mainnet python examples/stacks_m1_demo.py
```

The script prints the verdict, the txid and the explorer link. Five
payments have gone through this exact path from the pilot wallet
`SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5`, each an
`sbtc-token::transfer` to `SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C`:

| when | how | txid |
|---|---|---|
| 2026-09-07 | by hand | [`30689b5ee9f779fe571f0b7797e2453b21cf7d220cadf4f1881586757347387f`](https://explorer.hiro.so/txid/0x30689b5ee9f779fe571f0b7797e2453b21cf7d220cadf4f1881586757347387f?chain=mainnet) |
| 2026-09-07 | by hand | [`d1de1a792e2a23f5a0b22651eec4f3f00a49f3e9ca15c70c2a19fc8fb474dd8e`](https://explorer.hiro.so/txid/0xd1de1a792e2a23f5a0b22651eec4f3f00a49f3e9ca15c70c2a19fc8fb474dd8e?chain=mainnet) |
| 2026-09-08 | by hand | [`59ce70146da57ffd8c71cd67eea9169373ad11b7298e77a88e5f946a0064e325`](https://explorer.hiro.so/txid/0x59ce70146da57ffd8c71cd67eea9169373ad11b7298e77a88e5f946a0064e325?chain=mainnet) |
| 2026-09-14 | daily agent | [`3b51b4fe4d7f1fc7ec932dc8731dea4002216f53f615db7f5f114d97818f5650`](https://explorer.hiro.so/txid/0x3b51b4fe4d7f1fc7ec932dc8731dea4002216f53f615db7f5f114d97818f5650?chain=mainnet) |
| 2026-09-14 | daily agent | [`052058ec7988bdf5846c651c94e6c643549dc4a4070b28959a9f9f31452e1525`](https://explorer.hiro.so/txid/0x052058ec7988bdf5846c651c94e6c643549dc4a4070b28959a9f9f31452e1525?chain=mainnet) |

Step 4 takes the third one apart; the same commands work on any of them.

What happened, in order: the SDK asked for the 402 above, checked the
quote against the $0.05 cap (and against a floor BTC/USD rate, so a gateway
cannot inflate the sats past what the USD cap bounds), built an
`sbtc-token::transfer` of exactly `amount_sats` to `pay_to` with the
challenge id in the memo and a deny-mode post-condition for exactly that
amount, signed it, and sent the signed bytes in the `payment-signature`
header. The gateway recomputed the txid from the bytes, verified the
signature and every field against its own quote, broadcast to Hiro, waited
for confirmation, and only then ran the tool.

## 4. Verify the receipt two ways

The point of a receipt is that you do not have to trust the party that
issued it. Take the txid from step 3 (the one below is
`59ce7014…`, a receipt from 2026-09-08).

**On chain.** Hiro explorer:
`https://explorer.hiro.so/txid/0x59ce70146da57ffd8c71cd67eea9169373ad11b7298e77a88e5f946a0064e325?chain=mainnet`.
Or the API, which shows the memo carrying the challenge id:

```bash
curl -s https://api.hiro.so/extended/v1/tx/0x59ce70146da57ffd8c71cd67eea9169373ad11b7298e77a88e5f946a0064e325 | python -c "
import json,sys; t=json.load(sys.stdin); cc=t['contract_call']
print(t['tx_status'], t['block_height'], cc['contract_id'], cc['function_name'])
for a in cc['function_args']: print(' ', a['name'], a['repr'][:60])
print('post-conditions', [(p['type'], p['amount']) for p in t['post_conditions']])"
```

```
success 8944701 SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token transfer
  amount u13
  sender 'SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5
  recipient 'SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C
  memo (some 0x64353230666366372d303866612d343662302d623061302d30346435383534…
post-conditions [('fungible', '13')]
```

The memo bytes decode to `d520fcf7-08fa-46b0-b0a0-04d5854…`, the first 34
characters of the challenge's `payment_id`. That is the binding between
this transfer and this tool call, and it is checked exactly, not by
prefix.

**In the SDK's receipt.** `s.spending_summary()` carries the same txid
under `breakdown`, with `network: stacks-mainnet`, the USD amount charged
against the cap, and any `anomalies` (step 5). It is the client's record,
kept the moment value could leave the wallet, so a gateway that answered
nothing at all still leaves the spend on the books.

## 5. Refuse before signing

The cap bounds a session. Three rules bound each payment inside it, and
they are checked against the 402 (the payee and the amount that would be
signed) before the transaction exists:

```python
from agentpay import AgentWallet, Session, PolicyRejected, ApprovalRequired

wallet = AgentWallet(network="mainnet", stacks_key=os.environ["STACKS_AGENT_KEY"])
with Session(wallet, max_spend="0.05", prefer_chain="stacks",
             allowed_recipients=["SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C"],
             max_per_call="0.02",
             approve_above="0.01", approver=lambda req: input(f"{req} — pay? [y/N] ") == "y") as s:
    ...
```

The runnable version puts the gateway's payee outside the allowlist, sets
the per-call maximum below the price, and denies then approves at the
threshold, so three refusals happen with nothing signed and one payment
settles:

```bash
STACKS_NETWORK=mainnet python examples/stacks_policy_demo.py
```

The three refusals need no funds at all, which is the point: nothing is
signed. This is the demo's output on mainnet from a freshly generated key
with a zero balance (2026-09-14; the fourth scenario, the approved
settlement, is the payment in step 3):

```
payer (Stacks mainnet): SP2EV0HE…   gateway: https://agentpay.tools

====================================================================
1) RECIPIENT ALLOWLIST  ->  PAYEE NOT LISTED, NOTHING SIGNED
====================================================================
allowed_recipients=[SP0000000000…]   calling pre_trade_check ...
  ✓ refused before signing — rule=allowed_recipients chain=stacks payee=SP23XKWSEQ9D…
  spent: $0 | anomalies: [{'flag': 'policy_rejected', 'rule': 'allowed_recipients', 'count': 1, 'detail': '1 call(s) refused by allowed_recipients'}]

====================================================================
2) PER-CALL MAXIMUM  ->  OVER THE RULE, REFUSED ON THE QUOTE
====================================================================
max_per_call=$0.005 (tool is $0.01)   calling pre_trade_check ...
  ✓ refused — rule=max_per_call amount=$0.01
  spent: $0 | anomalies: [{'flag': 'policy_rejected', 'rule': 'max_per_call', 'count': 1, 'detail': '1 call(s) refused by max_per_call'}]

====================================================================
3) APPROVAL GATE  ->  DENIED, THEN APPROVED AND SETTLED IN sBTC
====================================================================
approve_above=$0.005 (tool is $0.01)   calling pre_trade_check ...
  approver asked: {'tool': 'pre_trade_check', 'chain': 'stacks', 'pay_to': 'SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C', 'amount_usd': '0.01', 'threshold': '0.005'}  -> no
  ✓ held — 'pre_trade_check' asks $0.01 USD, above the session's approve_above threshold of $0.005 and not approved — refusing to sign
  spent: $0 | anomalies: [{'flag': 'approval_required', 'rule': 'approve_above', 'count': 1, 'detail': '1 call(s) refused by approve_above'}]
```

The first refusal happened *after* the 402 was received (the payee is
only known then); the second happened on the registry's price, before the
tool was ever requested. In neither case was the wallet's nonce fetched.

Every refusal is on the receipt under `anomalies`, next to flags the SDK
raises on its own: the same paid call repeated, a single call taking half
the cap, a leg left unconfirmed, a paid leg that failed.

## 6. When a block is slow

The gateway waits for confirmation inside the request, under a 75-second
bound. If the transaction is still pending when the window closes, the call
raises `SettlementUncertain`: the spend is recorded, the transaction is live
on chain, the gateway keeps a row for it in state `uncertain`. Nothing is
lost:

```python
from agentpay import SettlementUncertain

try:
    r = s.call("pre_trade_check", {"symbol": "BTC"})
except SettlementUncertain as e:
    r = s.redeem(e, wait_s=600)        # waits for confirmation, re-presents the same signed tx once
```

If the process that signed is gone, the txid alone is enough
(`s.redeem_txid(txid, tool, params)`), because the signed bytes and the
memo are read back from Hiro. The demo in step 3 does this automatically,
and `STACKS_REDEEM_TXID=<txid>` reruns it for a transaction you already
sent. Details and the SDK's rule for a gateway that claims `rejected` after
broadcasting are in [`stacks-mainnet.md`](stacks-mainnet.md#when-the-gateway-cannot-confirm-in-time).

## 7. How settlement works, and what it does not do yet

**Sign, don't broadcast.** The agent signs; the gateway broadcasts to Hiro
directly. A Stacks x402 facilitator can be configured in front of that
(`STACKS_FACILITATOR_URL`) but is not required and not a dependency: the
payment artifact is a complete signed transaction, so anyone can broadcast
it and the deny-mode post-condition guarantees it moves exactly the quoted
sats to exactly the quoted payee or aborts. Handing it to an untrusted
broadcaster is safe.

**Fees are the payer's, in STX.** The 402 suggests `fee_microstx` (Hiro's
medium tier for an sBTC transfer, floored at 3,000 and capped at 20,000
µSTX); the SDK clamps whatever a gateway suggests to 0.05 STX. So a payer
wallet needs a little STX alongside its sBTC.

**Not yet:** sponsored (relay-paid) transactions are refused at
verification, so an sBTC-only wallet with no STX cannot pay today; one
in-flight Stacks payment per wallet, since nonces are sequential and the
SDK serialises signing; refunds on Stacks are not issued by the gateway (a
paid call whose tool fails reports `refund_unavailable` rather than
pretending), because the gateway holds no key to send from. All three are
stated in the known-limitations section of the reference.

**What a Stacks payer can buy.** Every tool in AgentPay's own catalogue
settles in sBTC, `verified_route` included. What `verified_route` *returns*
is another matter: it vets third-party x402 sellers, and the marketplace
it sweeps (Coinbase's Bazaar) lists Base and Solana sellers today, none on
Stacks. So a Stacks-only wallet can pay for the recommendation but not act
on it without a Base wallet as well. The payment rail is Stacks-native; the
marketplace is not yet. That changes the day the first Stacks x402 sellers
are in the sweep, and it is listed as the next integration in the grant's
closing update rather than glossed here.

## 8. An agent doing this on its own

The flagship analyst is a daily Railway cron that opens a $0.25 session,
reads free market data, buys `pre_trade_check` verdicts, and publishes its
reasoning to the ledger. Since 2026-09-14 it settles on the Stacks rail
(`FLAGSHIP_STACKS_KEY`, `FLAGSHIP_RAIL=stacks`), so the ledger's Stacks
receipts grow without anyone touching a key: each run card on
[agentpay.tools/ledger](https://agentpay.tools/ledger) shows the goal, the
verdicts bought, and the legs, each linking to explorer. The run's
reasoning names the Stacks address as payer, and a run whose settle was
uncertain waits and redeems the same transaction rather than dropping the
verdict. The first such run (2026-09-14 13:04 UTC) bought two verdicts
for 13 sats each, both `sbtc-token::transfer` from the pilot wallet to the
gateway's payee:

- SOL — [`3b51b4fe4d7f1fc7ec932dc8731dea4002216f53f615db7f5f114d97818f5650`](https://explorer.hiro.so/txid/0x3b51b4fe4d7f1fc7ec932dc8731dea4002216f53f615db7f5f114d97818f5650?chain=mainnet), block 8988117
- AVAX — [`052058ec7988bdf5846c651c94e6c643549dc4a4070b28959a9f9f31452e1525`](https://explorer.hiro.so/txid/0x052058ec7988bdf5846c651c94e6c643549dc4a4070b28959a9f9f31452e1525?chain=mainnet), block 8988119

Either resolves on Hiro without going through the ledger; the ledger's
run card for that day links the same two. Source: [`agents/analyst/`](../agents/analyst/).

## 9. For reviewers: what to attack

The four things a Stacks engineer has told us they would test first, and
where each one is answered. Every claim below has a test next to it; the
suite runs on every push.

**The wire, not the SDK.** The full exchange is four messages:

1. `POST /tools/<tool>/call` with the JSON envelope from step 2 and no
   payment → `402` whose `payment_options` lists every rail the gateway
   settles (`base`, `stellar`, `stacks`); the client picks one.
2. The client builds and signs the `sbtc-token::transfer` and retries the
   same POST, same body, with one header added, `payment-signature`, whose
   value is base64 of

   ```json
   {"x402Version": 2, "scheme": "exact", "network": "stacks:1",
    "payment_id": "<the 402's payment_id>",
    "accepted": {"asset": "sbtc", "amount_sats": 14, "pay_to": "SP23XK…"},
    "payload": {"signedTransaction": "<hex of the signed tx>", "txid": "<sha512/256 of it>"}}
   ```

3. The gateway decodes the bytes, recomputes the txid (never trusts the
   one in the payload), verifies, broadcasts, polls Hiro.
4. `200` with `{"tool", "result", "payment": {"amount_usdc", "tx_hash", "network": "stacks-mainnet"}}`,
   or `503` with `payment_status: "uncertain"` (step 6), or `402` again
   with a `reason` such as `wrong_recipient`, `memo_payment_id_mismatch`,
   `post_condition_mode_not_deny`, `invalid_origin_signature` or
   `payment_id_already_used_replay`. `examples/stacks_m1_demo.py` prints the txid so
   the same call can be checked on the explorer.

**The cap is enforced against the thing that moves value.** Before signing,
the SDK checks the 402's USD amount against the cap and the per-call rules,
then checks the *sats* two ways: they must be consistent with the USD at the
402's own rate, and they must not exceed what the USD could buy at a floor
BTC/USD rate the SDK will not let a gateway lower (`STACKS_MIN_BTC_USD`,
$20,000 on mainnet; an env override can only raise it). So a gateway
quoting `amount_usdc: 0.01, amount_sats: 100000` is refused with nothing
signed. The residual against a fully hostile gateway is the floor itself:
at most cap ÷ 20,000 BTC per call, stated as such in
[`stacks-m1.md`](stacks-m1.md#known-limitations). The cases, all in
`tests/test_stacks_cap_binds_signature.py` and
`tests/test_stacks_sdk.py`: inflated sats; a sub-floor rate; a missing rate;
sats that disagree with the quoted USD; cap exactly equal to the price
(signs, lands on the cap); cap one micro-dollar below (refused before the
nonce is fetched); a re-quote after a stale nonce that changes the
recipient or crosses the approval threshold
(`tests/test_stacks_stale_nonce_requote.py`).

The one to see with your own eyes, because it needs no funds and no
gateway: the guard is a pure function, so a hostile quote can be checked
in three lines.

```bash
python -c "
from agentpay._stacks_tx import assert_sats_within_cap
for sats in (13, 14, 100_000):
    try: assert_sats_within_cap(sats, '0.01', '76752', network='mainnet'); print(sats, '-> ok, sign')
    except ValueError as e: print(sats, '->', e)"
```

```
13 -> ok, sign
14 -> ok, sign
100000 -> amount_sats=100000 exceeds the most sats $0.01 could buy at the $20000/BTC floor (50) - refusing to sign
```

And the same case end to end through the client against a mocked gateway,
asserting that the wallet's nonce endpoint was never called and the
recorded spend is zero (the signer needs the nonce, so "not fetched" means
"not invoked"):

```bash
pytest tests/test_stacks_sdk.py -k "disagree_with_the_quoted or one_unit_below_the_price or exactly_equal_to_the_price" -v
```

```
tests/test_stacks_sdk.py::TestStacksHardRequirement::test_sats_that_disagree_with_the_quoted_usd_are_refused PASSED
tests/test_stacks_sdk.py::TestStacksHardRequirement::test_cap_one_unit_below_the_price_refuses_before_signing PASSED
tests/test_stacks_sdk.py::TestStacksHardRequirement::test_cap_exactly_equal_to_the_price_signs PASSED
3 passed
```

Repeated calls at the
boundary are bounded by the session's reservation model: each in-flight
call holds its quote against the cap from the moment it is accepted until
it settles or fails, so two concurrent calls cannot both fit in headroom
for one (`tests/test_budget_policy_stacks.py`, `tests/test_agentpay_sdk.py`).

**What binds what.** The signed transaction itself carries the recipient
(the gateway's payee), the exact amount (as a deny-mode post-condition, so
a different amount aborts on chain), and the challenge id in the memo. The
gateway binds the rest at settle time: the challenge names the tool and the
price, the memo must equal `payment_id[:34]` exactly, the transfer's
recipient must be the gateway's own payee, `arg_sender` must equal the
transaction sender, and the origin signature is verified before anything is
consumed or broadcast (`gateway/stacks.py verify_stacks_payment`). The txid
is then consumed in an insert-only table whose conflict answer gates the
result, so the same artifact presented twice, or for another tool, or on
another challenge, is refused (`tests/test_stacks_gateway.py`:
`test_memo_for_other_challenge_rejected`, `test_wrong_recipient_rejected`,
`test_pc_amount_mismatch_rejected`, `test_forged_signature_rejected_before_consume`,
`test_replayed_txid_rejected_before_broadcast`, `test_redeem_for_another_tool_refused`).
A different gateway has a different payee, so the artifact cannot pay it.
What is *not* bound is the presenter: a valid artifact delivers its result
to whoever presents it first, so the header in flight is protected by TLS
and nothing else. That is the same posture as any bearer payment proof in
x402 and we state it rather than hide it.

**Failure semantics.** Broadcast and confirmation are not atomic from the
client's side, and the SDK never retries a payment blindly: a retry would
sign a new transaction with the next nonce and pay twice. Instead the call
ends `SettlementUncertain` with the spend already recorded, and `redeem`
re-presents the identical signed bytes once the chain shows `success`; the
gateway flips `uncertain → verified` in a compare-and-set and delivers once
(`test_redeem_delivers_once`, `test_redeem_race_loser_is_refused`,
`test_redeem_still_pending_is_uncertain_again`,
`test_redeem_aborted_tx_is_rejected_and_row_closed`). The SDK also refuses
to take a gateway's `rejected` on faith after a broadcast: it asks Hiro, and
unless the chain shows the transaction unknown or aborted the spend stays
on the books (`tests/test_stacks_sdk.py`). If the transaction aborted, the
row closes `rejected` and nothing moved.

## Reproduce it end to end

```bash
git clone https://github.com/romudille-bit/agentpay && cd agentpay
python -m venv .venv && source .venv/bin/activate
pip install "agentpay-x402[stacks]"
export STACKS_AGENT_KEY=<your mainnet payer key>       # or the derive-key line at the top
STACKS_NETWORK=mainnet python examples/stacks_m1_demo.py
STACKS_NETWORK=mainnet python examples/stacks_policy_demo.py
```

Two paid calls, $0.02 in sBTC plus fees. Paste the two txids into the
explorer URL in step 4. If anything on this page does not match what you
see, that is a bug in the page or the code, and either is a welcome issue
on the repo.
