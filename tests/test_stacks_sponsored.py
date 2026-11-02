"""Sponsored Stacks payments relayed through VelumX (gateway/stacks_sponsor.py).

Pins: a sponsored tx is refused unless a relay is configured; the 402 option
and accepts[] advertise sponsorship only when it is; the relayed txid (not
the payer-bytes hash) is what gets confirmed and returned; a relay 4xx is a
definitive rejection; an ambiguous relay answer is resolved by (sender,
nonce) on Hiro. Network I/O is respx-mocked.
"""

import httpx
import pytest
import respx

import gateway.stacks as stacks_pay
from gateway import stacks_sponsor
from gateway.config import settings
from tests.test_stacks_gateway import (  # noqa: F401 — reuses the autouse settings fixture
    EXPECTED_SATS, GATEWAY_ADDR, HIRO, PAYER_KEY, PAYMENT_ID,
    _header_for, _verify, stacks_settings,
)
from agentpay._stacks_tx import StacksKeypair, build_sbtc_transfer, sign_transaction, txid_of

VELUMX = "https://velumx.test/api/v1"
RELAYED = "ab" * 32


@pytest.fixture
def sponsor_on(monkeypatch):
    monkeypatch.setattr(settings, "STACKS_SPONSOR", "velumx")
    monkeypatch.setattr(settings, "VELUMX_API_KEY", "vx_test_key")
    monkeypatch.setattr(settings, "VELUMX_API_URL", VELUMX)


def _sponsored_tx(nonce=4) -> bytes:
    kp = StacksKeypair.from_secret(PAYER_KEY)
    unsigned = build_sbtc_transfer(
        sender=kp, recipient=GATEWAY_ADDR, amount_sats=EXPECTED_SATS,
        payment_id=PAYMENT_ID, nonce=nonce, fee_microstx=0,
        network="testnet", sponsored=True,
    )
    return sign_transaction(unsigned, kp)


def _hiro_tx(txid: str, status: str):
    return respx.get(f"{HIRO}/extended/v1/tx/0x{txid}").mock(
        return_value=httpx.Response(200, json={"tx_status": status}))


class TestVerify:
    async def test_refused_without_relay(self):
        auth = await _verify(_header_for(_sponsored_tx()))
        assert not auth["authorized"]
        assert auth["reason"] == "sponsored_not_supported"

    async def test_refused_with_relay_name_but_no_key(self, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_SPONSOR", "velumx")
        monkeypatch.setattr(settings, "VELUMX_API_KEY", "")
        auth = await _verify(_header_for(_sponsored_tx()))
        assert auth["reason"] == "sponsored_not_supported"

    async def test_accepted_with_relay(self, sponsor_on):
        tx = _sponsored_tx()
        auth = await _verify(_header_for(tx))
        assert auth["authorized"], auth
        assert auth["sponsored"] is True
        assert auth["nonce"] == 4
        assert auth["txid"] == txid_of(tx)


class TestOffer:
    def test_402_option_silent_by_default(self):
        opt = stacks_pay.stacks_402_option((1030, "100000"), "0.001", fee_microstx=500)
        assert "sponsorship" not in opt
        entry = stacks_pay.stacks_accepts_entry((1030, "100000"), "0.001", PAYMENT_ID, 300)
        assert "sponsoredAccepted" not in entry["extra"]

    def test_402_option_advertises_when_on(self, sponsor_on):
        opt = stacks_pay.stacks_402_option((1030, "100000"), "0.001", fee_microstx=500)
        assert opt["sponsorship"]["available"] is True
        assert opt["sponsorship"]["origin_fee_microstx"] == 0
        assert opt["sponsorship"]["relay"] == "velumx"
        entry = stacks_pay.stacks_accepts_entry((1030, "100000"), "0.001", PAYMENT_ID, 300)
        assert entry["extra"]["sponsoredAccepted"] is True
        stx = stacks_pay.stacks_accepts_entry_stx((31152, "0.32"), "0.01", PAYMENT_ID, 300)
        assert stx["extra"]["sponsoredAccepted"] is True


class TestSettle:
    @respx.mock
    async def test_relay_accepted_then_confirmed(self, sponsor_on):
        tx = _sponsored_tx()
        origin = txid_of(tx)
        relay = respx.post(f"{VELUMX}/broadcast").mock(
            return_value=httpx.Response(200, json={"txid": "0x" + RELAYED}))
        _hiro_tx(RELAYED, "success")
        out = await stacks_pay.settle_stacks_payment(
            tx, origin, payment_id=PAYMENT_ID, sponsored=True,
            sender=StacksKeypair.from_secret(PAYER_KEY).address("testnet"), nonce=4)
        assert out["ok"] and out["state"] == "ok"
        assert out["txid"] == RELAYED            # the on-chain txid, not the payer-bytes hash
        assert relay.called
        body = relay.calls[0].request
        assert body.headers["x-api-key"] == "vx_test_key"
        assert b'"network": "testnet"' in body.content or b'"network":"testnet"' in body.content
        assert origin in stacks_pay._used_stacks_txids   # consume key stays the payer hash

    @respx.mock
    async def test_relay_4xx_is_definitive_rejection(self, sponsor_on):
        tx = _sponsored_tx()
        respx.post(f"{VELUMX}/broadcast").mock(
            return_value=httpx.Response(402, json={"error": "Insufficient relayer balance"}))
        hiro = _hiro_tx(RELAYED, "success")
        out = await stacks_pay.settle_stacks_payment(
            tx, txid_of(tx), payment_id=PAYMENT_ID, sponsored=True, sender="ST1", nonce=4)
        assert not out["ok"] and out["state"] == "rejected"
        assert out["reason"].startswith("sponsor_rejected: Insufficient relayer balance")
        assert not hiro.called

    @respx.mock
    async def test_relay_unreachable_is_rejection(self, sponsor_on):
        tx = _sponsored_tx()
        respx.post(f"{VELUMX}/broadcast").mock(side_effect=httpx.ConnectError("refused"))
        out = await stacks_pay.settle_stacks_payment(
            tx, txid_of(tx), payment_id=PAYMENT_ID, sponsored=True, sender="ST1", nonce=4)
        assert out["state"] == "rejected"
        assert out["reason"].startswith("sponsor_unreachable")

    @respx.mock
    async def test_relay_5xx_resolved_by_sender_nonce(self, sponsor_on):
        tx = _sponsored_tx(nonce=7)
        sender = StacksKeypair.from_secret(PAYER_KEY).address("testnet")
        respx.post(f"{VELUMX}/broadcast").mock(
            return_value=httpx.Response(502, json={"error": "upstream timeout"}))
        respx.get(f"{HIRO}/extended/v1/address/{sender}/mempool?limit=50").mock(
            return_value=httpx.Response(200, json={"results": [
                {"tx_id": "0x" + "cd" * 32, "nonce": 6, "sender_address": sender},
                {"tx_id": "0x" + RELAYED, "nonce": 7, "sender_address": sender},
            ]}))
        _hiro_tx(RELAYED, "success")
        out = await stacks_pay.settle_stacks_payment(
            tx, txid_of(tx), payment_id=PAYMENT_ID, sponsored=True, sender=sender, nonce=7)
        assert out["ok"] and out["txid"] == RELAYED

    @respx.mock
    async def test_relay_5xx_unresolved_is_uncertain(self, sponsor_on):
        tx = _sponsored_tx(nonce=7)
        sender = StacksKeypair.from_secret(PAYER_KEY).address("testnet")
        respx.post(f"{VELUMX}/broadcast").mock(return_value=httpx.Response(503, text="down"))
        respx.get(url__regex=rf"{HIRO}/extended/v1/address/{sender}/.*").mock(
            return_value=httpx.Response(200, json={"results": []}))
        out = await stacks_pay.settle_stacks_payment(
            tx, txid_of(tx), payment_id=PAYMENT_ID, sponsored=True, sender=sender, nonce=7)
        assert out["state"] == "uncertain"
        assert out["txid"] == txid_of(tx)
        assert out["reason"].startswith("sponsor_http_503")

    @respx.mock
    async def test_relay_accepted_then_aborted(self, sponsor_on):
        tx = _sponsored_tx()
        respx.post(f"{VELUMX}/broadcast").mock(
            return_value=httpx.Response(200, json={"txid": RELAYED}))
        _hiro_tx(RELAYED, "abort_by_post_condition")
        out = await stacks_pay.settle_stacks_payment(
            tx, txid_of(tx), payment_id=PAYMENT_ID, sponsored=True, sender="ST1", nonce=4)
        assert out["state"] == "rejected" and out["txid"] == RELAYED

    async def test_replay_of_payer_bytes_dies_before_relay(self, sponsor_on):
        tx = _sponsored_tx()
        stacks_pay._remember_txid(txid_of(tx))
        out = await stacks_pay.settle_stacks_payment(
            tx, txid_of(tx), payment_id=PAYMENT_ID, sponsored=True, sender="ST1", nonce=4)
        assert out["reason"] == "replay_attack"


class TestHelpers:
    def test_is_sponsored(self):
        assert stacks_pay.is_sponsored(_sponsored_tx())
        kp = StacksKeypair.from_secret(PAYER_KEY)
        plain = sign_transaction(build_sbtc_transfer(
            sender=kp, recipient=GATEWAY_ADDR, amount_sats=10, payment_id=PAYMENT_ID,
            nonce=1, fee_microstx=500, network="testnet"), kp)
        assert not stacks_pay.is_sponsored(plain)

    @respx.mock
    async def test_find_relayed_tx_falls_through_to_confirmed(self):
        sender = "ST1SENDER"
        respx.get(f"{HIRO}/extended/v1/address/{sender}/mempool?limit=50").mock(
            return_value=httpx.Response(200, json={"results": []}))
        respx.get(f"{HIRO}/extended/v1/address/{sender}/transactions?limit=50").mock(
            return_value=httpx.Response(200, json={"results": [
                {"tx": {"tx_id": "0x" + RELAYED, "nonce": 3, "sender_address": sender}}]}))
        assert await stacks_sponsor.find_relayed_tx(HIRO, sender, 3) == RELAYED
        assert await stacks_sponsor.find_relayed_tx(HIRO, sender, 9) is None
