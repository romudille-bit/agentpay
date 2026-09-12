"""
test_stacks_gateway.py — AGE-23: gateway/stacks.py verify + settle.

The gateway is the BROADCASTER on the Stacks rail: the client hands over a
fully signed, unbroadcast sBTC transfer; every failure mode between broadcast
and confirmation lands here. These tests pin the security order:

  verify   — Clarity decode, memo→payment_id binding (#5), recipient/amount,
             the MANDATORY sent-equal post-condition (refused, not repaired),
             txid recomputed from the bytes (header's copy never trusted)
  settle   — atomic pre-broadcast txid consume, fail-closed on infra (#6);
             facilitator → direct-Hiro degradation (kill-the-facilitator);
             ambiguous outcome → poll by txid → ok_recovered;
             DEFINITIVE node rejection → "rejected" (and only then)
  routing  — _settle_stacks_path binds payload.payment_id to the pending
             challenge and returns the payment_status bodies the SDK's
             retry logic keys on

Transactions are built with the real SDK signing lib (agentpay._stacks_tx),
so the gateway decodes genuine SIP-005 bytes. Network I/O is respx-mocked.
"""

import base64
import json
from decimal import Decimal

import httpx
import pytest
import respx

import gateway.stacks as stacks_pay
from agentpay._stacks_tx import (
    PostCondition,
    SBTC_CONTRACT_TESTNET,
    StacksKeypair,
    build_sbtc_transfer,
    sign_transaction,
    txid_of,
)
from gateway.config import settings
from gateway.services import supabase as sb

# Throwaway fixture keys (NEVER fund) — same as tests/fixtures.
PAYER_KEY = "000000000000000000000000000000000000000000000000000000000000000101"
GATEWAY_STACKS_KEY = "b244296d5907de9864c0b0d51f98a13c52890be0404e83f273144cd5b9960eed01"

PAYER = StacksKeypair.from_secret(PAYER_KEY)
GATEWAY_ADDR = StacksKeypair.from_secret(GATEWAY_STACKS_KEY).address("testnet")

PAYMENT_ID = "3f6f2b04-7a1e-4c1d-9d2a-active00test"
HIRO = "https://api.testnet.hiro.so"
FACILITATOR = "https://stacks-facilitator.example"


@pytest.fixture(autouse=True)
def stacks_settings(monkeypatch):
    """Configure the gateway for Stacks testnet + fast polls; reset the
    in-memory txid consume set; disable Supabase by default."""
    monkeypatch.setattr(settings, "STACKS_ENABLED", True)
    monkeypatch.setattr(settings, "STACKS_NETWORK", "testnet")
    monkeypatch.setattr(settings, "STACKS_GATEWAY_ADDRESS", GATEWAY_ADDR)
    monkeypatch.setattr(settings, "STACKS_HIRO_API", "")
    monkeypatch.setattr(settings, "STACKS_FACILITATOR_URL", "")
    monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "100000")
    monkeypatch.setattr(settings, "STACKS_CONFIRM_POLL_S", 0.01)
    monkeypatch.setattr(settings, "STACKS_CONFIRM_MAX_POLLS", 3)
    monkeypatch.setattr(settings, "STACKS_SETTLE_TIMEOUT_S", 5.0)
    monkeypatch.setattr(settings, "STACKS_RATE_CACHE_S", 60.0)
    monkeypatch.setattr(sb, "sb_enabled", lambda: False)
    stacks_pay._used_stacks_txids.clear()
    stacks_pay._rate_cache["rate"] = None
    stacks_pay._rate_cache["at"] = 0.0
    # AGE-135: cancel any in-flight background rate refresh so it can't write
    # into another test's cache.
    if stacks_pay._rate_refresh_task is not None:
        stacks_pay._rate_refresh_task.cancel()
        stacks_pay._rate_refresh_task = None
    yield
    stacks_pay._used_stacks_txids.clear()
    stacks_pay._rate_cache["rate"] = None
    stacks_pay._rate_cache["at"] = 0.0
    stacks_pay._fee_cache["fee"] = None
    stacks_pay._fee_cache["at"] = 0.0
    if stacks_pay._rate_refresh_task is not None:
        stacks_pay._rate_refresh_task.cancel()
        stacks_pay._rate_refresh_task = None


COINGECKO = r"https://api\.coingecko\.com/api/v3/simple/price.*"


def _mock_coingecko(usd):
    return respx.get(url__regex=COINGECKO).mock(
        return_value=httpx.Response(200, json={"bitcoin": {"usd": usd}})
    )


def _signed_tx(amount_sats=1030, payment_id=PAYMENT_ID, recipient=None,
               network="testnet", key=PAYER_KEY, nonce=4, fee=500) -> bytes:
    kp = StacksKeypair.from_secret(key)
    unsigned = build_sbtc_transfer(
        sender=kp,
        recipient=recipient or GATEWAY_ADDR,
        amount_sats=amount_sats,
        payment_id=payment_id,
        nonce=nonce,
        fee_microstx=fee,
        network=network,
    )
    return sign_transaction(unsigned, kp)


def _header_for(signed_tx: bytes, payment_id=PAYMENT_ID, network="stacks:2147483648") -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": network,
        "payment_id": payment_id,
        "payload": {"signedTransaction": signed_tx.hex(), "txid": txid_of(signed_tx)},
        "accepted": {"scheme": "exact", "network": network, "asset": "sbtc",
                     "payTo": GATEWAY_ADDR},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


EXPECTED_SATS = 1030  # matches _signed_tx default


async def _verify(header, **over):
    kw = dict(expected_amount_sats=EXPECTED_SATS,
              expected_recipient=GATEWAY_ADDR, payment_id=PAYMENT_ID)
    kw.update(over)
    return await stacks_pay.verify_stacks_payment(header, **kw)


# ── decoding ─────────────────────────────────────────────────────────────────


class TestDecode:

    def test_roundtrip_of_sdk_built_tx(self):
        tx = _signed_tx()
        d = stacks_pay.decode_sbtc_transfer(tx)
        assert d["network"] == "testnet"
        assert d["sender"] == PAYER.address("testnet")
        assert d["contract_id"] == SBTC_CONTRACT_TESTNET
        assert d["function"] == "transfer"
        assert d["amount"] == 1030
        assert d["arg_sender"] == PAYER.address("testnet")
        assert d["arg_recipient"] == GATEWAY_ADDR
        assert d["memo"].decode() == PAYMENT_ID[:34]
        assert d["nonce"] == 4 and d["fee"] == 500
        assert len(d["post_conditions"]) == 1
        pc = d["post_conditions"][0]
        assert pc["condition_code"] == 0x01 and pc["amount"] == 1030
        assert pc["asset_contract"] == SBTC_CONTRACT_TESTNET

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            stacks_pay.decode_sbtc_transfer(b"\x80\x80\x00")
        with pytest.raises(ValueError):
            stacks_pay.decode_sbtc_transfer(_signed_tx() + b"\x00")  # trailing bytes


# ── verification ─────────────────────────────────────────────────────────────


class TestVerify:

    async def test_happy_path_recomputes_txid(self):
        tx = _signed_tx()
        auth = await _verify(_header_for(tx))
        assert auth["authorized"] is True
        assert auth["txid"] == txid_of(tx)          # recomputed, not copied
        assert auth["sender"] == PAYER.address("testnet")
        assert auth["amount_sats"] == 1030
        assert auth["overpaid"] is False

    async def test_header_txid_is_ignored(self):
        """A lying client txid must not survive — verification recomputes."""
        tx = _signed_tx()
        payload = json.loads(base64.b64decode(_header_for(tx)))
        payload["payload"]["txid"] = "f" * 64  # lie
        header = base64.b64encode(json.dumps(payload).encode()).decode()
        auth = await _verify(header)
        assert auth["authorized"] and auth["txid"] == txid_of(tx)

    async def test_memo_binding_rejected_on_mismatch(self):
        # [CHECKLIST #5] — the tx pays the right amount to the right address,
        # but for a DIFFERENT challenge. Must be refused.
        tx = _signed_tx(payment_id="a-completely-different-challenge-id")
        auth = await _verify(_header_for(tx))
        assert not auth["authorized"]
        assert auth["reason"] == "memo_payment_id_mismatch"

    async def test_memo_truncation_prefix_rule(self):
        # UUIDs are 36 chars; the (buff 34) memo truncates to 34 — the
        # truncated form must still bind.
        long_pid = "c0ffee00-1111-2222-3333-444455556666"  # 36 chars
        tx = _signed_tx(payment_id=long_pid)
        auth = await _verify(_header_for(tx, payment_id=long_pid),
                             payment_id=long_pid)
        assert auth["authorized"], auth["reason"]

    async def test_short_memo_does_not_bind_a_longer_id(self):
        # AGE-153: a 1-byte memo used to satisfy the prefix rule for any
        # challenge id starting with that byte. Exact match on the
        # truncated id now.
        tx = _signed_tx(payment_id="c")
        auth = await _verify(_header_for(tx, payment_id="c0ffee00-1111"),
                             payment_id="c0ffee00-1111")
        assert not auth["authorized"]
        assert auth["reason"] == "memo_payment_id_mismatch"

    async def test_wrong_recipient_rejected(self):
        other = StacksKeypair.from_secret(
            "edf9aee84d9b7abc145504dde6726c64f369d37ee34ded868fabd876c26570bc01"
        ).address("testnet")
        tx = _signed_tx(recipient=other)
        auth = await _verify(_header_for(tx))
        assert not auth["authorized"]
        assert auth["reason"] == "wrong_recipient"

    async def test_underpaid_rejected(self):
        tx = _signed_tx(amount_sats=900)
        auth = await _verify(_header_for(tx))
        assert not auth["authorized"]
        assert auth["reason"].startswith("underpaid")

    async def test_small_quote_has_no_usable_tolerance(self):
        """At micro-prices the 2% allowance rounds to whole sats, so the exact
        amount is required: truncating the floor turned it into 10% on a 10-sat
        quote and 25% on a 4-sat one."""
        tx = _signed_tx(amount_sats=9)
        auth = await _verify(_header_for(tx),
                             expected_amount_sats=10)
        assert not auth["authorized"]
        assert auth["reason"].startswith("underpaid")

        tx_exact = _signed_tx(amount_sats=10)
        auth_ok = await _verify(_header_for(tx_exact),
                                expected_amount_sats=10)
        assert auth_ok["authorized"]

    async def test_tolerance_still_absorbs_drift_on_a_real_quote(self):
        """Above the threshold a 2% shortfall is still accepted — the tolerance
        exists to absorb FX drift between quote and verification."""
        tx = _signed_tx(amount_sats=990)
        auth = await _verify(_header_for(tx),
                             expected_amount_sats=1000)
        assert auth["authorized"]

    async def test_overpay_flagged_not_rejected(self):
        tx = _signed_tx(amount_sats=5000)
        auth = await _verify(_header_for(tx))
        assert auth["authorized"] and auth["overpaid"] is True

    async def test_wrong_network_rejected(self):
        tx = _signed_tx(network="mainnet",
                        recipient=StacksKeypair.from_secret(GATEWAY_STACKS_KEY).address("mainnet"))
        auth = await _verify(_header_for(tx))
        assert not auth["authorized"]
        assert auth["reason"] == "wrong_network"

    async def test_missing_post_condition_rejected(self):
        """Surgery: strip the post-condition from real signed bytes. The
        gateway must refuse to broadcast an unguarded transfer."""
        tx = _signed_tx()
        pc_bytes = PostCondition(
            sender=PAYER.address("testnet"),
            contract=SBTC_CONTRACT_TESTNET,
            amount_sats=1030,
        ).serialize()
        pc_block = b"\x02" + (1).to_bytes(4, "big") + pc_bytes
        assert pc_block in tx
        stripped = tx.replace(pc_block, b"\x02" + (0).to_bytes(4, "big"))
        auth = await _verify(_header_for(stripped))
        assert not auth["authorized"]
        assert auth["reason"] == "unsafe_post_conditions"

    async def test_pc_amount_mismatch_rejected(self):
        """Surgery: post-condition asserts fewer sats than the transfer
        moves — the guard would not protect the payer. Refused."""
        tx = _signed_tx()
        good_pc = PostCondition(
            sender=PAYER.address("testnet"),
            contract=SBTC_CONTRACT_TESTNET, amount_sats=1030,
        ).serialize()
        bad_pc = PostCondition(
            sender=PAYER.address("testnet"),
            contract=SBTC_CONTRACT_TESTNET, amount_sats=1,
        ).serialize()
        tampered = tx.replace(good_pc, bad_pc)
        auth = await _verify(_header_for(tampered))
        assert not auth["authorized"]
        assert auth["reason"] == "unsafe_post_conditions"

    async def test_forged_signature_rejected_before_consume(self):
        tx = bytearray(_signed_tx())
        tx[6 + 38 + 20] ^= 0x01
        auth = await _verify(_header_for(bytes(tx)))
        assert not auth["authorized"]
        assert auth["reason"] == "invalid_origin_signature"

    async def test_unsigned_tx_rejected(self):
        unsigned = build_sbtc_transfer(
            sender=PAYER, recipient=GATEWAY_ADDR, amount_sats=1030,
            payment_id=PAYMENT_ID, nonce=4, fee_microstx=500, network="testnet",
        )
        auth = await _verify(_header_for(unsigned))
        assert not auth["authorized"]
        assert auth["reason"] == "invalid_origin_signature"

    async def test_uncompressed_payer_key_accepted(self):
        tx = _signed_tx(key=PAYER_KEY[:64])
        auth = await _verify(_header_for(tx))
        assert auth["authorized"], auth["reason"]
        assert auth["sender"] == StacksKeypair.from_secret(PAYER_KEY[:64]).address("testnet")

    async def test_garbage_header_rejected(self):
        auth = await _verify("not!!base64@@")
        assert not auth["authorized"]

    async def test_non_transfer_payload_rejected(self):
        tx = _signed_tx()
        auth = await _verify(_header_for(tx[:-40]))  # truncated
        assert not auth["authorized"]
        assert auth["reason"].startswith("malformed_stacks_tx")


# ── settlement ───────────────────────────────────────────────────────────────


def _hiro_broadcast_ok():
    return respx.post(f"{HIRO}/v2/transactions").mock(
        return_value=httpx.Response(200, json="0" * 64)
    )


def _hiro_status(*statuses):
    responses = [httpx.Response(200, json={"tx_status": s}) if s != 404
                 else httpx.Response(404) for s in statuses]
    # Pinned to the 0x form: Hiro 302-redirects the bare-hex path, and a
    # regex mock hid that the poll was asking for it.
    return respx.get(url__regex=rf"{HIRO}/extended/v1/tx/0x[0-9a-f]{{64}}$").mock(
        side_effect=responses + [responses[-1]] * 10
    )


class TestSettle:

    async def test_direct_broadcast_confirms_ok(self):
        tx = _signed_tx()
        with respx.mock:
            bcast = _hiro_broadcast_ok()
            _hiro_status(404, "pending", "success")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert res["ok"] and res["state"] == "ok"
        assert bcast.call_count == 1

    async def test_poll_asks_hiro_for_the_0x_form(self):
        # Hiro answers the bare-hex path with a 302 to 0x…; a poll that asked
        # for the bare form read every confirmed tx as "not yet".
        tx = _signed_tx()
        txid = txid_of(tx)
        with respx.mock:
            bare = respx.get(f"{HIRO}/extended/v1/tx/{txid}").mock(
                return_value=httpx.Response(302, headers={
                    "location": f"/extended/v1/tx/0x{txid}"}))
            good = respx.get(f"{HIRO}/extended/v1/tx/0x{txid}").mock(
                return_value=httpx.Response(200, json={"tx_status": "success"}))
            res = await stacks_pay.poll_confirmation(txid, max_polls=1)
        assert res["status"] == "success"
        assert good.call_count == 1 and bare.call_count == 0

    async def test_replayed_txid_rejected_before_broadcast(self):
        # [CHECKLIST #6]: second settle of the same txid must die BEFORE any
        # network I/O — no Hiro route mocked, so a broadcast would error.
        tx = _signed_tx()
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("success")
            res1 = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
            assert res1["ok"]
        with respx.mock:  # no routes: any I/O would fail loudly
            res2 = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert not res2["ok"]
        assert res2["state"] == "rejected"
        assert res2["reason"] == "replay_attack"

    async def test_durable_consume_conflict_is_replay(self, monkeypatch):
        monkeypatch.setattr(sb, "sb_enabled", lambda: True)
        async def _conflict(txh, net):
            return False
        monkeypatch.setattr(sb, "record_tx_hash", _conflict)
        tx = _signed_tx()
        with respx.mock:
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert res["state"] == "rejected" and res["reason"] == "replay_attack"

    async def test_durable_consume_outage_fails_closed_as_rejected(self, monkeypatch):
        """Infra outage → refuse to broadcast, release the in-memory hold,
        report "rejected" with a re-sign hint (AGE-153): nothing reached a
        node, and the SDK confirms that on Hiro before zeroing the leg."""
        monkeypatch.setattr(sb, "sb_enabled", lambda: True)
        async def _outage(txh, net):
            return None
        monkeypatch.setattr(sb, "record_tx_hash", _outage)
        tx = _signed_tx()
        with respx.mock:  # no broadcast route: broadcasting would error
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert res["state"] == "rejected"
        assert res["reason"].startswith("replay_store_unavailable")
        assert txid_of(tx) not in stacks_pay._used_stacks_txids  # released

    async def test_definitive_rejection_bad_nonce(self):
        tx = _signed_tx()
        with respx.mock:
            respx.post(f"{HIRO}/v2/transactions").mock(
                return_value=httpx.Response(400, json={
                    "error": "transaction rejected",
                    "reason": "BadNonce",
                    "reason_data": {"expected": 9, "actual": 4},
                })
            )
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert not res["ok"]
        assert res["state"] == "rejected"
        assert "BadNonce" in res["reason"]

    async def test_broadcast_transport_error_recovers_if_confirmed(self):
        """The ok_recovered lesson: the POST dies but the tx actually made it
        — poll by txid, find it confirmed, never charge-for-nothing."""
        tx = _signed_tx()
        with respx.mock:
            respx.post(f"{HIRO}/v2/transactions").mock(
                side_effect=httpx.ConnectError("mid-flight death"))
            _hiro_status("success")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert res["ok"] and res["state"] == "ok_recovered"

    async def test_accepted_but_never_confirms_is_uncertain(self):
        tx = _signed_tx()
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("pending", "pending", "pending")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert not res["ok"]
        assert res["state"] == "uncertain"
        assert res["reason"] == "broadcast_accepted_pending_confirmation"

    async def test_settle_deadline_answers_uncertain(self, monkeypatch):
        # AGE-150: the whole broadcast+confirm is bounded; past the deadline
        # the reply is a structured "uncertain", never an empty edge 524.
        monkeypatch.setattr(settings, "STACKS_SETTLE_DEADLINE_S", 0.05)
        monkeypatch.setattr(settings, "STACKS_CONFIRM_POLL_S", 0.5)
        tx = _signed_tx()
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("pending", "pending", "pending")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert res["state"] == "uncertain"
        assert res["reason"] == "settle_deadline_pending_confirmation"
        assert res["txid"] == txid_of(tx)
        assert txid_of(tx) in stacks_pay._used_stacks_txids   # consume stands

    async def test_dropped_is_not_definitive(self):
        # AGE-152 (adjacent): dropped_* bytes are still valid; keep polling
        # and end uncertain rather than telling the SDK to zero the leg.
        tx = _signed_tx()
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("dropped_replace_by_fee", "dropped_replace_by_fee")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert res["state"] == "uncertain"
        assert res["reason"] == "broadcast_accepted_pending_confirmation"

    async def test_abort_by_post_condition_is_rejected(self):
        tx = _signed_tx()
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("pending", "abort_by_post_condition")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID)
        assert res["state"] == "rejected"
        assert res["reason"] == "abort_by_post_condition"

    async def test_facilitator_settles_then_confirmed(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_FACILITATOR_URL", FACILITATOR)
        tx = _signed_tx()
        with respx.mock:
            fac = respx.post(f"{FACILITATOR}/settle").mock(
                return_value=httpx.Response(200, json={"success": True}))
            _hiro_status("success")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID,
                payment_payload={"payload": {}}, requirements={})
        assert res["ok"] and res["state"] == "ok"
        assert fac.call_count == 1

    async def test_kill_the_facilitator_degrades_to_direct(self, monkeypatch):
        """Acceptance: facilitator dead → the SAME payment settles via direct
        Hiro broadcast. The facilitator is convenience, not a dependency."""
        monkeypatch.setattr(settings, "STACKS_FACILITATOR_URL", FACILITATOR)
        tx = _signed_tx()
        with respx.mock:
            respx.post(f"{FACILITATOR}/settle").mock(
                side_effect=httpx.ConnectError("facilitator is dead"))
            bcast = _hiro_broadcast_ok()
            _hiro_status("success")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID,
                payment_payload={"payload": {}}, requirements={})
        assert res["ok"] and res["state"] == "ok"
        assert bcast.call_count == 1

    async def test_facilitator_ambiguous_recovers_from_chain(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_FACILITATOR_URL", FACILITATOR)
        tx = _signed_tx()
        with respx.mock:
            respx.post(f"{FACILITATOR}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": False,
                    "errorReason": "timeout waiting for confirmation",
                }))
            _hiro_status(404, "success")
            res = await stacks_pay.settle_stacks_payment(
                tx, txid_of(tx), payment_id=PAYMENT_ID,
                payment_payload={"payload": {}}, requirements={})
        assert res["ok"] and res["state"] == "ok_recovered"


# ── 402 option + quote ───────────────────────────────────────────────────────


class TestQuoteAndOption:

    async def test_option_shape_live_rate(self):
        with respx.mock:
            cg = _mock_coingecko(100000)
            opt = await stacks_pay.build_stacks_402_option(
                "0.001", "https://x/tools/t/call")
        assert cg.call_count == 1
        assert opt["network"] == "stacks:2147483648"
        assert opt["amount_usdc"] == "0.001"
        assert opt["pay_to"] == GATEWAY_ADDR
        assert opt["btc_usd_rate"] == "100000"
        # $0.001 at $100k/BTC = 1 sat exactly (ceil rule)
        assert opt["amount_sats"] == 1

    async def test_ceils_never_undercharges(self):
        # $0.001 at $97,123/BTC = 1.029… sats → 2, never 1.
        with respx.mock:
            _mock_coingecko(97123)
            sats = await stacks_pay.stacks_quote_sats("0.001")
        assert sats == 2

    async def test_zero_price_never_offers_stacks(self):
        # Returns before any rate fetch — no respx needed.
        assert await stacks_pay.build_stacks_402_option("0.000") is None
        assert await stacks_pay.build_stacks_402_option("0") is None

    async def test_unconfigured_gateway_offers_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_ENABLED", False)
        assert await stacks_pay.build_stacks_402_option("0.001") is None
        # Configured but no rate available (live fails + no fixed fallback):
        monkeypatch.setattr(settings, "STACKS_ENABLED", True)
        monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "")
        with respx.mock:  # CoinGecko unmocked → live fetch fails
            assert await stacks_pay.build_stacks_402_option("0.001") is None


class TestLiveRate:

    async def test_fallback_to_fixed_when_live_fails(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "80000")
        with respx.mock:  # CoinGecko unmocked → raises → fixed fallback
            sats = await stacks_pay.stacks_quote_sats("0.01")
        # $0.01 at $80k = 12.5 sats → 13 (ceil)
        assert sats == 13

    async def test_no_source_at_all_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "")
        with respx.mock:
            assert await stacks_pay.stacks_quote_sats("0.01") is None

    async def test_rate_is_cached(self):
        with respx.mock:
            cg = _mock_coingecko(100000)
            r1 = await stacks_pay._btc_usd_rate()
            r2 = await stacks_pay._btc_usd_rate()
        assert r1 == r2 == Decimal("100000")
        assert cg.call_count == 1   # second call served from cache

    async def test_cache_expires_stale_while_revalidate(self, monkeypatch):
        # AGE-135: an EXPIRED cache no longer blocks the caller on CoinGecko —
        # the stale value is served immediately and a single-flight background
        # task refreshes the cache.
        monkeypatch.setattr(settings, "STACKS_RATE_CACHE_S", 0.0)  # never fresh
        with respx.mock:
            cg = _mock_coingecko(100000)
            r1 = await stacks_pay._btc_usd_rate()      # cold boot: blocking fetch
            r2 = await stacks_pay._btc_usd_rate()      # stale: served immediately
            assert r1 == r2 == Decimal("100000")
            assert cg.call_count == 1                  # no inline second fetch
            assert stacks_pay._rate_refresh_task is not None
            await stacks_pay._rate_refresh_task        # background refresh ran
        assert cg.call_count == 2

    async def test_stale_refresh_is_single_flight(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_RATE_CACHE_S", 0.0)
        with respx.mock:
            cg = _mock_coingecko(100000)
            await stacks_pay._btc_usd_rate()           # prime (1 fetch)
            await stacks_pay._btc_usd_rate()           # spawns refresh task
            task = stacks_pay._rate_refresh_task
            await stacks_pay._btc_usd_rate()           # must NOT spawn a second
            assert stacks_pay._rate_refresh_task is task
            await task
        assert cg.call_count == 2

    async def test_cold_boot_fetch_failure_falls_back_to_fixed(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "50000")
        with respx.mock:  # no route mocked → fetch raises
            r = await stacks_pay._btc_usd_rate()
        assert r == Decimal("50000")

    async def test_live_preferred_over_fixed(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "1")  # absurd floor
        with respx.mock:
            _mock_coingecko(100000)
            sats = await stacks_pay.stacks_quote_sats("0.001")
        assert sats == 1   # used the live 100k, not the fixed 1


class TestQuoteStableAcrossFxMove:
    """The sats quoted at 402-issuance ride on the challenge and are what
    settle verifies against — a BTC move between issue and settle must not
    fail the amount check, and the quote survives a restart because the
    challenge row carries it (AGE-95)."""

    class _Tool:
        name = "verified_route"
        price_usdc = "0.01"

    async def test_quote_rides_on_the_challenge(self):
        from gateway.x402 import issue_payment_challenge, _pending_challenges
        with respx.mock:
            _mock_coingecko(100000)
            quote = await stacks_pay.stacks_quote("0.01")
        assert quote[0] == 10
        ch = issue_payment_challenge(
            tool_name="verified_route", price_usdc="0.01", developer_address="",
            request_data={}, persist=False, stacks_quote=quote,
        )
        stored = _pending_challenges.pop(ch.payment_id)
        assert stored["stacks_sats"] == 10 and stored["stacks_rate"] == "100000"
        opt = stacks_pay.stacks_402_option(quote, "0.01")
        assert opt["amount_sats"] == 10 and opt["btc_usd_rate"] == "100000"

    def test_supabase_row_roundtrip(self):
        from gateway.x402 import _normalize_supabase_challenge
        row = {"payment_id": PAYMENT_ID, "tool_name": "verified_route",
               "amount_usdc": "0.01", "expires_at": "2099-01-01T00:00:00+00:00",
               "stacks_sats": 10, "stacks_rate": "100000"}
        ch = _normalize_supabase_challenge(row)
        assert ch["stacks_sats"] == 10 and ch["stacks_rate"] == "100000"
        row.update(stacks_sats=None, stacks_rate=None)
        ch = _normalize_supabase_challenge(row)
        assert ch["stacks_sats"] is None and ch["stacks_rate"] is None

    async def test_settle_uses_issuance_quote_not_requote(self, monkeypatch):
        import gateway.routes.tools as rt

        # Agent signs a tx paying the QUOTED 10 sats ($0.01 @ $100k).
        tx = _signed_tx(amount_sats=10, payment_id=PAYMENT_ID)
        header = _header_for(tx, payment_id=PAYMENT_ID)
        payload = json.loads(base64.b64decode(header))

        # Settle while BTC has HALVED to $50k (a re-quote would demand 20
        # sats and reject the 10-sat tx as underpaid). The challenge — as
        # read back from Supabase after a restart — carries the quote.
        challenge = {"payment_id": PAYMENT_ID, "tool_name": "verified_route",
                     "amount_usdc": "0.01", "expires_at": 9999999999.0,
                     "stacks_sats": 10, "stacks_rate": "100000"}
        async def _lookup(pid):
            return challenge if pid == PAYMENT_ID else None
        monkeypatch.setattr(rt, "_lookup_challenge", _lookup)
        async def _noop(*a, **k):
            return None
        monkeypatch.setattr(rt, "update_payment_log_state", _noop)
        monkeypatch.setattr(rt, "sb_enabled", lambda: False)

        with respx.mock:
            _mock_coingecko(50000)          # rate moved — must be ignored
            _hiro_broadcast_ok()
            _hiro_status("success")
            auth = await rt._settle_stacks_path(
                self._Tool(), "verified_route", header, payload)

        assert isinstance(auth, dict) and auth["authorized"]
        assert auth["amount_sats"] == 10
        assert auth["btc_usd_rate"] == "100000"


# ── route-level binding (_settle_stacks_path) ────────────────────────────────


class TestSettleStacksPath:
    """Exercises the routes/tools.py glue without the FastAPI app: challenge
    binding, the payment_status bodies the SDK keys on, and the auth dict."""

    class _Tool:
        name = "verified_route"
        price_usdc = "0.001"

    @pytest.fixture(autouse=True)
    def _route_mocks(self, monkeypatch):
        import gateway.routes.tools as rt
        self.rt = rt
        # 1 sat @ $100k on the challenge — matches _header's amount_sats=1.
        self.challenge = {
            "payment_id": PAYMENT_ID,
            "tool_name": "verified_route",
            "amount_usdc": "0.001",
            "expires_at": 9999999999.0,
            "stacks_sats": 1,
            "stacks_rate": "100000",
        }
        async def _lookup(pid):
            return self.challenge if pid == PAYMENT_ID else None
        monkeypatch.setattr(rt, "_lookup_challenge", _lookup)
        async def _noop_update(*a, **k):
            return None
        monkeypatch.setattr(rt, "update_payment_log_state", _noop_update)
        monkeypatch.setattr(rt, "sb_enabled", lambda: False)
        yield

    def _header(self, amount_sats=1, payment_id=PAYMENT_ID):
        tx = _signed_tx(amount_sats=amount_sats, payment_id=payment_id)
        return _header_for(tx, payment_id=payment_id), tx

    async def test_happy_path_returns_auth(self):
        header, tx = self._header()
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("success")
            payload = json.loads(base64.b64decode(header))
            auth = await self.rt._settle_stacks_path(
                self._Tool(), "verified_route", header, payload)
        assert isinstance(auth, dict) and auth["authorized"]
        assert auth["tx_hash"] == txid_of(tx)
        assert auth["payer"] == PAYER.address("testnet")
        assert auth["network"] == "stacks-testnet"

    async def test_requotes_when_challenge_has_no_quote(self):
        # Challenge issued without a Stacks quote (pre-AGE-95 row, or the
        # option was omitted): settle re-quotes at the live rate.
        self.challenge.pop("stacks_sats")
        self.challenge.pop("stacks_rate")
        header, tx = self._header(amount_sats=1)   # 0.001 @ $100k = 1 sat
        payload = json.loads(base64.b64decode(header))
        with respx.mock:
            _mock_coingecko(100000)
            _hiro_broadcast_ok()
            _hiro_status("success")
            auth = await self.rt._settle_stacks_path(
                self._Tool(), "verified_route", header, payload)
        assert isinstance(auth, dict) and auth["authorized"]
        assert auth["btc_usd_rate"] == "100000"

    async def test_unknown_payment_id_is_rejected_body(self):
        header, _ = self._header(payment_id="99999999-aaaa-bbbb-cccc-dddddddddddd")
        payload = json.loads(base64.b64decode(header))
        resp = await self.rt._settle_stacks_path(
            self._Tool(), "verified_route", header, payload)
        assert resp.status_code == 402
        body = json.loads(resp.body)
        assert body["payment_status"] == "rejected"
        assert body["error_reason"] == "unknown_or_expired_payment_id"

    async def test_memo_for_other_challenge_rejected(self):
        # header names OUR challenge, but the tx memo binds a different one.
        tx = _signed_tx(amount_sats=1, payment_id="another-challenge-entirely")
        header = _header_for(tx, payment_id=PAYMENT_ID)
        payload = json.loads(base64.b64decode(header))
        resp = await self.rt._settle_stacks_path(
            self._Tool(), "verified_route", header, payload)
        assert resp.status_code == 402
        assert json.loads(resp.body)["error_reason"] == "memo_payment_id_mismatch"

    async def test_expired_challenge_rejected(self):
        self.challenge["expires_at"] = 1.0
        header, _ = self._header()
        payload = json.loads(base64.b64decode(header))
        resp = await self.rt._settle_stacks_path(
            self._Tool(), "verified_route", header, payload)
        assert json.loads(resp.body)["error_reason"] == "challenge_expired"

    async def test_settle_rejection_maps_to_rejected_body(self):
        header, _ = self._header()
        payload = json.loads(base64.b64decode(header))
        with respx.mock:
            respx.post(f"{HIRO}/v2/transactions").mock(
                return_value=httpx.Response(400, json={
                    "error": "transaction rejected", "reason": "ConflictingNonceInMempool",
                }))
            resp = await self.rt._settle_stacks_path(
                self._Tool(), "verified_route", header, payload)
        assert resp.status_code == 402
        body = json.loads(resp.body)
        assert body["payment_status"] == "rejected"
        assert "ConflictingNonceInMempool" in body["error_reason"]

    async def test_uncertain_maps_to_503_with_txid(self):
        header, tx = self._header()
        payload = json.loads(base64.b64decode(header))
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("pending", "pending", "pending")
            resp = await self.rt._settle_stacks_path(
                self._Tool(), "verified_route", header, payload)
        assert resp.status_code == 503
        body = json.loads(resp.body)
        assert body["payment_status"] == "uncertain"
        assert body["txid"] == txid_of(tx)
        assert body["payment_id"] == PAYMENT_ID

    async def test_unconfigured_gateway_503s(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(settings, "STACKS_ENABLED", False)
        header, _ = self._header()
        payload = json.loads(base64.b64decode(header))
        with pytest.raises(HTTPException) as exc:
            await self.rt._settle_stacks_path(
                self._Tool(), "verified_route", header, payload)
        assert exc.value.status_code == 503


class TestSponsoredAndPostConditionMode:
    """A sponsored tx (can never broadcast) and allow-mode post-conditions are
    both refused before settlement."""

    async def test_sponsored_tx_refused(self):
        # Real sponsored bytes: built + signed with the sponsored auth variant.
        kp = StacksKeypair.from_secret(PAYER_KEY)
        unsigned = build_sbtc_transfer(
            sender=kp, recipient=GATEWAY_ADDR, amount_sats=EXPECTED_SATS,
            payment_id=PAYMENT_ID, nonce=4, fee_microstx=500,
            network="testnet", sponsored=True,
        )
        tx = sign_transaction(unsigned, kp)
        auth = await _verify(_header_for(tx))
        assert not auth["authorized"]
        assert auth["reason"] == "sponsored_not_supported"

    async def test_allow_mode_refused(self, monkeypatch):
        tx = _signed_tx()
        decoded = stacks_pay.decode_sbtc_transfer(tx)
        monkeypatch.setattr(stacks_pay, "decode_sbtc_transfer",
                            lambda _b: {**decoded, "pc_mode": 0x01})
        auth = await _verify(_header_for(tx))
        assert not auth["authorized"]
        assert auth["reason"] == "post_condition_mode_not_deny"


# ── route-level: the 402 carries the option and the challenge carries the quote


class TestRoute402CarriesQuote:
    def test_paid_402_offers_stacks_and_stores_quote_on_challenge(self, client, monkeypatch):
        from gateway.x402 import _pending_challenges
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        with respx.mock(assert_all_called=False):
            _mock_coingecko(100000)
            r = client.post("/tools/pre_trade_check/call",
                            json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        body = r.json()
        opt = body["payment_options"]["stacks"]
        assert opt["btc_usd_rate"] == "100000"
        ch = _pending_challenges[body["payment_id"]]
        assert ch["stacks_sats"] == opt["amount_sats"]
        assert ch["stacks_rate"] == "100000"

    def test_free_tool_402_has_no_stacks_option(self, client):
        from gateway.x402 import _pending_challenges
        r = client.post("/tools/token_price/call", json={"parameters": {"symbol": "ETH"}})
        if r.status_code != 402:
            pytest.skip("token_price is not free-with-402 in this registry")
        body = r.json()
        assert "stacks" not in (body.get("payment_options") or {})
        assert _pending_challenges[body["payment_id"]]["stacks_sats"] is None


# ── fee suggestion (AGE-151) ─────────────────────────────────────────────────


def _hiro_fees(*fees):
    return respx.post(f"{HIRO}/v2/fees/transaction").mock(
        return_value=httpx.Response(200, json={
            "estimations": [{"fee_rate": 1, "fee": f} for f in fees]}))


class TestFeeSuggestion:
    async def test_offers_medium_tier_when_above_floor(self):
        # AGE-153: the middle of Hiro's three tiers, not the fast one.
        with respx.mock:
            _mock_coingecko(100000)
            _hiro_fees(300, 7590, 50850)
            opt = await stacks_pay.build_stacks_402_option("0.01")
        assert opt["fee_microstx"] == 7590

    async def test_floor_wins_when_estimate_is_low(self):
        with respx.mock:
            _mock_coingecko(100000)
            _hiro_fees(300, 481, 759)
            opt = await stacks_pay.build_stacks_402_option("0.01")
        assert opt["fee_microstx"] == settings.STACKS_SUGGESTED_FEE_MICROSTX == 3000

    async def test_cap_bounds_a_spiking_estimate(self):
        with respx.mock:
            _mock_coingecko(100000)
            _hiro_fees(1, 5_000_000, 5_000_000)
            opt = await stacks_pay.build_stacks_402_option("0.01")
        assert opt["fee_microstx"] == settings.STACKS_FEE_CAP_MICROSTX == 20_000

    async def test_no_estimate_falls_back_to_floor(self):
        with respx.mock:
            _mock_coingecko(100000)
            respx.post(f"{HIRO}/v2/fees/transaction").mock(
                return_value=httpx.Response(400, json={"reason": "NoEstimateAvailable"}))
            opt = await stacks_pay.build_stacks_402_option("0.01")
        assert opt["fee_microstx"] == 3000

    async def test_estimate_is_cached(self):
        with respx.mock:
            _mock_coingecko(100000)
            fees = _hiro_fees(300, 481, 7590)
            await stacks_pay.build_stacks_402_option("0.01")
            await stacks_pay.build_stacks_402_option("0.01")
        assert fees.call_count == 1

    async def test_disabled_estimate_uses_floor_without_io(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_FEE_ESTIMATE", False)
        with respx.mock:
            _mock_coingecko(100000)
            opt = await stacks_pay.build_stacks_402_option("0.01")
        assert opt["fee_microstx"] == 3000


# ── uncertain settle: row + redemption (AGE-147) ─────────────────────────────


class TestUncertainRedemption:
    class _Tool:
        name = "verified_route"
        price_usdc = "0.001"
        developer_address = ""

    @pytest.fixture(autouse=True)
    def _mocks(self, monkeypatch):
        import gateway.routes.tools as rt
        self.rt = rt
        self.inserted: list[dict] = []
        self.patched: list[tuple] = []
        self.rows: dict[str, dict] = {}
        self.challenge = {"payment_id": PAYMENT_ID, "tool_name": "verified_route",
                          "amount_usdc": "0.001", "expires_at": 9999999999.0,
                          "stacks_sats": 1, "stacks_rate": "100000"}
        self.pid_consumed = False

        async def _lookup(pid):
            return self.challenge if pid == PAYMENT_ID else None
        async def _insert(**kw):
            self.inserted.append(kw)
            self.rows[kw["payment_id"]] = dict(kw)
            return 1
        async def _get(pid, columns=None):
            return self.rows.get(pid)
        async def _update(pid, state, *, expected_state=None, clear_fields=None, **f):
            row = self.rows.get(pid)
            self.patched.append((pid, state, expected_state))
            if row is None or (expected_state and row["state"] != expected_state):
                return 0
            row["state"] = state
            return 1
        async def _record_pid(pid):
            return not self.pid_consumed
        async def _record_tx(txid, net):
            return True
        monkeypatch.setattr(rt, "_lookup_challenge", _lookup)
        monkeypatch.setattr(rt, "insert_pending_payment_log", _insert)
        monkeypatch.setattr(rt, "get_payment_log", _get)
        monkeypatch.setattr(rt, "update_payment_log_state", _update)
        monkeypatch.setattr(rt, "record_payment_id", _record_pid)
        monkeypatch.setattr(rt, "sb_enabled", lambda: True)
        monkeypatch.setattr(sb, "sb_enabled", lambda: True)
        monkeypatch.setattr(sb, "record_tx_hash", _record_tx)
        yield

    def _header(self):
        tx = _signed_tx(amount_sats=1, payment_id=PAYMENT_ID)
        h = _header_for(tx, payment_id=PAYMENT_ID)
        return h, json.loads(base64.b64decode(h)), txid_of(tx)

    async def _settle(self, header, payload):
        return await self.rt._settle_stacks_path(
            self._Tool(), "verified_route", header, payload, parameters={"q": 1})

    async def test_uncertain_writes_row_keyed_on_txid(self):
        header, payload, txid = self._header()
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("pending", "pending", "pending")
            resp = await self._settle(header, payload)
        assert resp.status_code == 503
        assert json.loads(resp.body)["redeem"]
        row = self.rows[txid]
        assert row["state"] == "uncertain" and row["tx_hash"] == txid
        assert row["agent_address"] == PAYER.address("testnet")
        assert PAYMENT_ID in row["error_reason"] and row["parameters"] == {"q": 1}

    async def _leave_uncertain(self):
        header, payload, txid = self._header()
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("pending", "pending", "pending")
            await self._settle(header, payload)
        assert self.rows[txid]["state"] == "uncertain"
        stacks_pay._used_stacks_txids.clear()
        return header, payload, txid

    async def test_redeem_after_challenge_expired(self):
        header, payload, txid = await self._leave_uncertain()
        self.challenge["expires_at"] = 1.0
        with respx.mock:
            _hiro_status("success")
            auth = await self._settle(header, payload)
        assert isinstance(auth, dict) and auth["redeemed"] and auth["tx_hash"] == txid
        assert auth["payer"] == PAYER.address("testnet")
        assert self.rows[txid]["state"] == "verified"

    async def test_redeem_when_payment_id_consumed(self):
        header, payload, txid = await self._leave_uncertain()
        self.pid_consumed = True
        with respx.mock:
            _hiro_status("success")
            auth = await self._settle(header, payload)
        assert isinstance(auth, dict) and auth["redeemed"]

    async def test_redeem_still_pending_is_uncertain_again(self):
        header, payload, txid = await self._leave_uncertain()
        self.pid_consumed = True
        with respx.mock:
            _hiro_status("pending", "pending")
            resp = await self._settle(header, payload)
        assert resp.status_code == 503
        assert json.loads(resp.body)["error_reason"] == "still_unconfirmed"
        assert self.rows[txid]["state"] == "uncertain"

    async def test_redeem_aborted_tx_is_rejected_and_row_closed(self):
        header, payload, txid = await self._leave_uncertain()
        self.pid_consumed = True
        with respx.mock:
            _hiro_status("abort_by_post_condition")
            resp = await self._settle(header, payload)
        assert resp.status_code == 402
        assert json.loads(resp.body)["payment_status"] == "rejected"
        assert self.rows[txid]["state"] == "rejected"

    async def test_redeem_delivers_once(self):
        header, payload, txid = await self._leave_uncertain()
        self.pid_consumed = True
        with respx.mock:
            _hiro_status("success", "success")
            first = await self._settle(header, payload)
            second = await self._settle(header, payload)
        assert isinstance(first, dict) and first["redeemed"]
        assert second.status_code == 402
        assert json.loads(second.body)["error_reason"] == "payment_id_already_used_replay"

    async def test_redeem_race_loser_is_refused(self, monkeypatch):
        header, payload, txid = await self._leave_uncertain()
        self.pid_consumed = True
        async def _lost_cas(pid, state, **kw):
            return 0
        monkeypatch.setattr(self.rt, "update_payment_log_state", _lost_cas)
        with respx.mock:
            _hiro_status("success")
            resp = await self._settle(header, payload)
        assert json.loads(resp.body)["error_reason"] == "already_redeemed"

    async def test_consumed_id_without_uncertain_row_is_a_replay(self):
        header, payload, txid = self._header()
        self.pid_consumed = True
        resp = await self._settle(header, payload)
        assert json.loads(resp.body)["error_reason"] == "payment_id_already_used_replay"

    async def test_redeem_for_another_tool_refused(self):
        header, payload, txid = await self._leave_uncertain()
        self.rows[txid]["tool_name"] = "pre_trade_check"
        self.pid_consumed = True
        with respx.mock:
            _hiro_status("success")
            resp = await self._settle(header, payload)
        assert json.loads(resp.body)["error_reason"] == "redeem_tool_mismatch"
