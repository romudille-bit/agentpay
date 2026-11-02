"""SDK side of sponsored Stacks payments.

The 402 advertises `payment_options.stacks.sponsorship`; the wallet then
signs the sponsored auth variant with fee 0 (auto), can be forced or turned
off, and the client tracks the relay's txid (reported by the gateway) rather
than the payer-bytes hash for chain checks and redemption.
"""

import httpx
import pytest
import respx
from stellar_sdk import Keypair

from agentpay._client import AgentPayClient
from agentpay._wallet import AgentWallet, PaymentFailed, SettlementUncertain
from tests.test_stacks_sdk import (
    GATEWAY, TOOL_URL, STACKS_KEY, _stacks_402, _ok_200, _decode_ps_header,
    _mock_nonce,
)

HIRO = "https://api.testnet.hiro.so"
RELAYED = "ab" * 32
SPONSORSHIP = {"available": True, "relay": "velumx", "origin_fee_microstx": 0}


def _wallet(**kw):
    return AgentWallet(secret_key=Keypair.random().secret, network="testnet",
                       stacks_key=STACKS_KEY, **kw)


def _ok_200_relayed():
    body = _ok_200()
    body["payment"]["tx_hash"] = RELAYED
    return body


class TestBuild:
    def test_auto_signs_sponsored_fee_zero_when_offered(self):
        w = _wallet()
        with respx.mock:
            _mock_nonce(3)
            built = w.build_stacks_payment(
                _stacks_402(sponsorship=SPONSORSHIP)["payment_options"]["stacks"],
                "pay_stacks_test_0001", TOOL_URL)
        payload, tx, nonce = _decode_ps_header(built["header"])
        assert built["sponsored"] is True
        assert tx[5] == 0x05                       # sponsored auth variant
        assert int.from_bytes(tx[35:43], "big") == 0  # origin fee 0
        assert payload["payload"]["sponsored"] is True
        assert built["sender"] == w.stacks_address and nonce == 3

    def test_auto_pays_own_fee_when_not_offered(self):
        w = _wallet()
        with respx.mock:
            _mock_nonce(3)
            built = w.build_stacks_payment(
                _stacks_402()["payment_options"]["stacks"], "pay_stacks_test_0001", TOOL_URL)
        _, tx, _ = _decode_ps_header(built["header"])
        assert built["sponsored"] is False and tx[5] == 0x04
        assert int.from_bytes(tx[35:43], "big") == 500

    def test_forced_requires_offer(self):
        w = _wallet(stacks_sponsored=True)
        with respx.mock:
            _mock_nonce(3)
            with pytest.raises(ValueError, match="offers no sponsorship"):
                w.build_stacks_payment(_stacks_402()["payment_options"]["stacks"],
                                       "pay_stacks_test_0001", TOOL_URL)

    def test_disabled_ignores_offer(self, monkeypatch):
        monkeypatch.setenv("STACKS_SPONSORED", "0")
        w = _wallet()
        assert w.stacks_sponsored is False
        with respx.mock:
            _mock_nonce(3)
            built = w.build_stacks_payment(
                _stacks_402(sponsorship=SPONSORSHIP)["payment_options"]["stacks"],
                "pay_stacks_test_0001", TOOL_URL)
        assert built["sponsored"] is False

    def test_env_auto_and_forced(self, monkeypatch):
        monkeypatch.setenv("STACKS_SPONSORED", "auto")
        assert _wallet().stacks_sponsored is None
        monkeypatch.setenv("STACKS_SPONSORED", "1")
        assert _wallet().stacks_sponsored is True
        monkeypatch.delenv("STACKS_SPONSORED")
        assert _wallet().stacks_sponsored is None


class TestClient:
    def test_settled_leg_records_the_relayed_txid(self):
        w = _wallet()
        client = AgentPayClient(wallet=w, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(7)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402(sponsorship=SPONSORSHIP)),
                httpx.Response(200, json=_ok_200_relayed()),
            ])
            result = client.call_tool("verified_route", {"need": "x"}, max_spend="0.0011",
                                      prefer_chain="stacks", chain_is_explicit=True)
        assert result["result"]["ok"] is True
        assert client.call_log[-1]["tx_hash"] == RELAYED
        assert client.call_log[-1]["state"] == "settled"

    def test_unsponsored_leg_keeps_the_payer_hash(self):
        w = _wallet()
        client = AgentPayClient(wallet=w, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(7)
            route = respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                httpx.Response(200, json=_ok_200_relayed()),
            ])
            client.call_tool("verified_route", {"need": "x"}, max_spend="0.0011",
                             prefer_chain="stacks", chain_is_explicit=True)
        payload, _, _ = _decode_ps_header(route.calls[-1].request.headers["payment-signature"])
        # A gateway txid is only trusted for sponsored legs.
        assert client.call_log[-1]["tx_hash"] == payload["payload"]["txid"]

    def test_uncertain_carries_relayed_txid_for_redeem(self):
        w = _wallet()
        client = AgentPayClient(wallet=w, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(7)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402(sponsorship=SPONSORSHIP)),
                httpx.Response(503, json={"payment_status": "uncertain",
                                          "error_reason": "relay_accepted_pending_confirmation",
                                          "payment_id": "pay_stacks_test_0001",
                                          "txid": RELAYED}),
            ])
            with pytest.raises(SettlementUncertain) as exc:
                client.call_tool("verified_route", {"need": "x"}, max_spend="0.0011",
                                 prefer_chain="stacks", chain_is_explicit=True)
        assert exc.value.tx_hash == RELAYED
        ctx = exc.value.redeem_ctx
        assert ctx["txid"] == RELAYED and ctx["sponsored"] and ctx["relayed_known"]
        assert ctx["sender"] == w.stacks_address and ctx["nonce"] == 7

    def test_rejected_sponsored_checks_chain_by_nonce(self):
        """The relay refused (nothing broadcast): Hiro has no tx at the nonce,
        so the leg is zeroed. The payer-bytes hash is never looked up."""
        w = _wallet()
        client = AgentPayClient(wallet=w, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(7)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402(sponsorship=SPONSORSHIP)),
                httpx.Response(402, json={"payment_status": "rejected",
                                          "error_reason": "sponsor_rejected: cap exceeded"}),
            ])
            by_hash = respx.get(url__regex=rf"{HIRO}/extended/v1/tx/0x.*").mock(
                return_value=httpx.Response(404))
            respx.get(url__regex=rf"{HIRO}/extended/v1/address/{w.stacks_address}/.*").mock(
                return_value=httpx.Response(200, json={"results": []}))
            with pytest.raises(PaymentFailed, match="sponsor_rejected"):
                client.call_tool("verified_route", {"need": "x"}, max_spend="0.0011",
                                 prefer_chain="stacks", chain_is_explicit=True)
        assert not by_hash.called
        assert client.call_log[-1]["amount_usdc"] == "0"

    def test_rejected_sponsored_but_nonce_live_keeps_spend(self):
        w = _wallet()
        client = AgentPayClient(wallet=w, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(7)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402(sponsorship=SPONSORSHIP)),
                httpx.Response(402, json={"payment_status": "rejected",
                                          "error_reason": "sponsor_http_502"}),
            ])
            respx.get(f"{HIRO}/extended/v1/address/{w.stacks_address}/mempool?limit=50").mock(
                return_value=httpx.Response(200, json={"results": [
                    {"tx_id": "0x" + RELAYED, "nonce": 7, "tx_status": "pending"}]}))
            with pytest.raises(SettlementUncertain):
                client.call_tool("verified_route", {"need": "x"}, max_spend="0.0011",
                                 prefer_chain="stacks", chain_is_explicit=True)
        assert client.call_log[-1]["amount_usdc"] != "0"


class TestRedeemResolvesRelayedTxid:
    def test_redeem_finds_relayed_txid_by_nonce(self):
        w = _wallet()
        client = AgentPayClient(wallet=w, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(7)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402(sponsorship=SPONSORSHIP)),
                httpx.Response(503, json={"payment_status": "uncertain",
                                          "error_reason": "sponsor_http_503: down",
                                          "payment_id": "pay_stacks_test_0001"}),
                httpx.Response(200, json=_ok_200_relayed()),
            ])
            with pytest.raises(SettlementUncertain) as exc:
                client.call_tool("verified_route", {"need": "x"}, max_spend="0.0011",
                                 prefer_chain="stacks", chain_is_explicit=True)
            assert not exc.value.redeem_ctx["relayed_known"]
            respx.get(f"{HIRO}/extended/v1/address/{w.stacks_address}/mempool?limit=50").mock(
                return_value=httpx.Response(200, json={"results": [
                    {"tx_id": "0x" + RELAYED, "nonce": 7, "tx_status": "pending"}]}))
            respx.get(f"{HIRO}/extended/v1/tx/0x{RELAYED}").mock(
                return_value=httpx.Response(200, json={"tx_status": "success"}))
            out = client.redeem(exc.value, wait_s=5, poll_s=0.01)
        assert out["result"]["ok"] is True
        assert exc.value.redeem_ctx["txid"] == RELAYED
