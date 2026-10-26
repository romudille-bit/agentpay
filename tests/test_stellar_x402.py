"""
Standard x402 `exact` on Stellar (gateway/stellar_x402.py + the route glue).

The facilitator does the chain work; these tests pin what the gateway adds:
the accepts[] entry it advertises, the verify → hold → settle order, the
settled-hash consume, and the payment_status bodies a client keys on.
Facilitator I/O is respx-mocked.
"""

import base64
import json

import httpx
import pytest
import respx

from gateway import stellar_x402 as sx
from gateway.config import settings
from gateway.services import supabase as sb

GW = "GBI6GZW2MDSZ6N5BN7JSDCTQQ6NEOC6PSDAVYTMYXWXOPUVWQ3O5E67S"
PAYER = "GAVWB3YVRWYFBDPPG7PJ4R4ZVMNGZE2UGGXIAAKW7MMVS5K4NQ7XV4EJ"
FAC = "https://facilitator.example/x402/testnet"
TX = "a" * 64
RESOURCE = "https://gw.example/tools/token_price/call"


@pytest.fixture(autouse=True)
def stellar_settings(monkeypatch):
    monkeypatch.setattr(settings, "STELLAR_NETWORK", "testnet")
    monkeypatch.setattr(settings, "GATEWAY_PUBLIC_KEY", GW)
    monkeypatch.setattr(settings, "STELLAR_STANDARD_CLIENTS", True)
    monkeypatch.setattr(settings, "STELLAR_FACILITATOR_API_KEY", "k")
    monkeypatch.setattr(settings, "STELLAR_FACILITATOR_TESTNET_URL", FAC)
    monkeypatch.setattr(settings, "STELLAR_FACILITATOR_TIMEOUT_S", 2.0)
    monkeypatch.setattr(settings, "STELLAR_USDC_SAC", "")
    monkeypatch.setattr(sb, "sb_enabled", lambda: False)
    sx._used_tx_hashes.clear()
    yield
    sx._used_tx_hashes.clear()


def _req(price="0.01"):
    return sx.payment_requirements(price, RESOURCE, "Token price")


def _payload(network="stellar:testnet", tx_xdr="AAAAAgAAAAB..."):
    return {
        "x402Version": 2,
        "resource": {"url": RESOURCE, "mimeType": "application/json"},
        "accepted": {**_req(), "network": network},
        "payload": {"transaction": tx_xdr},
    }


def _verify_ok(payer=PAYER):
    return respx.post(f"{FAC}/verify").mock(
        return_value=httpx.Response(200, json={"isValid": True, "payer": payer}))


def _settle_ok(tx=TX, payer=PAYER):
    return respx.post(f"{FAC}/settle").mock(return_value=httpx.Response(200, json={
        "success": True, "transaction": tx, "network": "stellar:testnet", "payer": payer}))


# ── requirements / payload helpers ───────────────────────────────────────────


class TestRequirements:
    def test_amount_is_seven_decimal_atomic(self):
        assert sx.usdc_atomic("0.01") == "100000"
        assert sx.usdc_atomic("0.001") == "10000"
        assert sx.usdc_atomic("1") == "10000000"

    def test_entry_shape_testnet(self):
        r = _req()
        assert r["scheme"] == "exact" and r["network"] == "stellar:testnet"
        assert r["asset"] == sx.USDC_SAC["stellar:testnet"]
        assert r["payTo"] == GW and r["amount"] == "100000"
        assert r["extra"] == {"areFeesSponsored": True}
        assert r["maxTimeoutSeconds"] == settings.STELLAR_MAX_TIMEOUT_S

    def test_entry_shape_pubnet(self, monkeypatch):
        monkeypatch.setattr(settings, "STELLAR_NETWORK", "mainnet")
        r = _req()
        assert r["network"] == "stellar:pubnet"
        assert r["asset"] == sx.USDC_SAC["stellar:pubnet"]
        assert sx.facilitator_url() == settings.STELLAR_FACILITATOR_URL.rstrip("/")

    def test_sac_override(self, monkeypatch):
        monkeypatch.setattr(settings, "STELLAR_USDC_SAC", "C" + "A" * 55)
        assert _req()["asset"] == "C" + "A" * 55

    def test_unconfigured_without_key_or_flag(self, monkeypatch):
        assert sx.configured()
        monkeypatch.setattr(settings, "STELLAR_FACILITATOR_API_KEY", "")
        assert not sx.configured()
        monkeypatch.setattr(settings, "STELLAR_FACILITATOR_API_KEY", "k")
        monkeypatch.setattr(settings, "STELLAR_STANDARD_CLIENTS", False)
        assert not sx.configured()

    def test_payload_detection(self):
        assert sx.is_standard_payload(_payload())
        assert sx.payload_network(_payload()) == "stellar:testnet"
        # The SDK's classic proof never travels as a payment-signature
        # payload, and a stacks payload is not ours.
        assert not sx.is_standard_payload({"accepted": {"network": "stacks:1"},
                                           "payload": {"transaction": "00"}})
        assert not sx.is_standard_payload({"accepted": {"network": "stellar:testnet"},
                                           "payload": {}})


# ── facilitator client ───────────────────────────────────────────────────────


class TestVerify:
    async def test_valid_returns_payer(self):
        with respx.mock:
            route = _verify_ok()
            vr = await sx.verify(_payload(), _req())
        assert vr == {"ok": True, "payer": PAYER, "reason": "ok", "unavailable": False}
        sent = json.loads(route.calls[0].request.content)
        assert sent["x402Version"] == 2 and sent["paymentRequirements"]["payTo"] == GW
        assert route.calls[0].request.headers["authorization"] == "Bearer k"

    async def test_invalid_carries_reason(self):
        with respx.mock:
            respx.post(f"{FAC}/verify").mock(return_value=httpx.Response(200, json={
                "isValid": False, "invalidReason": "invalid_exact_stellar_payload_amount",
                "payer": PAYER}))
            vr = await sx.verify(_payload(), _req())
        assert not vr["ok"] and vr["reason"] == "invalid_exact_stellar_payload_amount"
        assert not vr["unavailable"]

    async def test_5xx_is_unavailable(self):
        with respx.mock:
            respx.post(f"{FAC}/verify").mock(return_value=httpx.Response(502))
            vr = await sx.verify(_payload(), _req())
        assert not vr["ok"] and vr["unavailable"] and vr["reason"] == "facilitator_http_502"

    async def test_401_is_a_rejection_we_can_read(self):
        with respx.mock:
            respx.post(f"{FAC}/verify").mock(return_value=httpx.Response(401, json={
                "error": "Unauthorized"}))
            vr = await sx.verify(_payload(), _req())
        assert not vr["ok"] and vr["unavailable"] and vr["reason"] == "facilitator_http_401"


class TestSettle:
    async def test_success_consumes_hash(self):
        with respx.mock:
            _settle_ok()
            s = await sx.settle(_payload(), _req())
        assert s["success"] and s["tx_hash"] == TX and s["payer"] == PAYER
        assert s["network"] == "stellar:testnet" and not s["uncertain"]
        assert TX in sx._used_tx_hashes

    async def test_same_hash_twice_is_replay(self):
        with respx.mock:
            _settle_ok()
            await sx.settle(_payload(), _req())
            s = await sx.settle(_payload(), _req())
        assert not s["success"] and s["reason"] == "replay_attack" and not s["uncertain"]

    async def test_durable_conflict_is_replay(self, monkeypatch):
        async def _conflict(tx, net):
            return False
        monkeypatch.setattr(sb, "record_tx_hash", _conflict)
        with respx.mock:
            _settle_ok()
            s = await sx.settle(_payload(), _req())
        assert s["reason"] == "replay_attack"

    async def test_durable_outage_fails_closed_and_releases(self, monkeypatch):
        async def _down(tx, net):
            return None
        monkeypatch.setattr(sb, "record_tx_hash", _down)
        with respx.mock:
            _settle_ok()
            s = await sx.settle(_payload(), _req())
        assert not s["success"] and s["reason"].startswith("replay_check_unavailable")
        assert s["uncertain"] and TX not in sx._used_tx_hashes

    async def test_definitive_rejection_is_not_uncertain(self):
        with respx.mock:
            respx.post(f"{FAC}/settle").mock(return_value=httpx.Response(200, json={
                "success": False, "errorReason": "invalid_exact_stellar_payload_auth_entry_expired",
                "payer": PAYER}))
            s = await sx.settle(_payload(), _req())
        assert not s["success"] and not s["uncertain"]
        assert s["reason"] == "invalid_exact_stellar_payload_auth_entry_expired"

    async def test_timeout_is_uncertain(self):
        with respx.mock:
            respx.post(f"{FAC}/settle").mock(side_effect=httpx.ReadTimeout("slow"))
            s = await sx.settle(_payload(), _req())
        assert not s["success"] and s["uncertain"] and s["reason"] == "facilitator_timeout"

    async def test_unknown_failure_is_uncertain(self):
        with respx.mock:
            respx.post(f"{FAC}/settle").mock(return_value=httpx.Response(200, json={
                "success": False, "errorReason": "settlement_pending", "transaction": TX}))
            s = await sx.settle(_payload(), _req())
        assert s["uncertain"] and s["tx_hash"] == TX

    async def test_malformed_success_is_not_trusted(self):
        with respx.mock:
            _settle_ok(tx="0xdeadbeef")
            s = await sx.settle(_payload(), _req())
        assert not s["success"] and s["reason"] == "facilitator_malformed_transaction"
        assert not sx._used_tx_hashes


# ── route glue ───────────────────────────────────────────────────────────────


class TestSettleStellarStandardPath:
    class _Tool:
        name = "token_price"
        price_usdc = "0.01"
        description = "Token price"
        developer_address = GW

    @pytest.fixture(autouse=True)
    def _route(self, monkeypatch):
        import gateway.routes.tools as rt
        self.rt = rt
        monkeypatch.setattr(rt, "sb_enabled", lambda: False)
        yield

    async def _call(self, payload=None):
        return await self.rt._settle_stellar_standard_path(
            self._Tool(), "token_price", payload or _payload(), RESOURCE)

    async def test_happy_path_returns_auth(self):
        with respx.mock:
            v, s = _verify_ok(), _settle_ok()
            auth = await self._call()
        assert isinstance(auth, dict) and auth["authorized"]
        assert auth == {"authorized": True, "tx_hash": TX, "payer": PAYER,
                        "network": "stellar-testnet", "session": None}
        assert v.called and s.called

    async def test_verify_rejection_never_settles(self):
        with respx.mock:
            respx.post(f"{FAC}/verify").mock(return_value=httpx.Response(200, json={
                "isValid": False, "invalidReason": "invalid_exact_stellar_payload_recipient"}))
            s = _settle_ok()
            resp = await self._call()
        assert resp.status_code == 402
        body = json.loads(resp.body)
        assert body["payment_status"] == "rejected"
        assert body["error_reason"] == "invalid_exact_stellar_payload_recipient"
        assert not s.called

    async def test_facilitator_down_is_503_rejected(self):
        with respx.mock:
            respx.post(f"{FAC}/verify").mock(return_value=httpx.Response(503))
            resp = await self._call()
        assert resp.status_code == 503
        assert json.loads(resp.body)["payment_status"] == "rejected"

    async def test_settle_uncertain_is_503_with_redeem_hint(self):
        with respx.mock:
            _verify_ok()
            respx.post(f"{FAC}/settle").mock(side_effect=httpx.ReadTimeout("slow"))
            resp = await self._call()
        assert resp.status_code == 503
        body = json.loads(resp.body)
        assert body["payment_status"] == "uncertain"
        assert "same payment-signature" in body["redeem"]

    async def test_network_mismatch_rejected_before_facilitator(self):
        with respx.mock:
            v = _verify_ok()
            resp = await self._call(_payload(network="stellar:pubnet"))
        assert resp.status_code == 402
        assert json.loads(resp.body)["error_reason"] == "network_mismatch"
        assert not v.called

    async def test_unconfigured_503s(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(settings, "STELLAR_FACILITATOR_API_KEY", "")
        with pytest.raises(HTTPException) as ei:
            await self._call()
        assert ei.value.status_code == 503


# ── 402 advertisement ────────────────────────────────────────────────────────


class Test402Entry:
    def _entries(self, r):
        body = r.json()["accepts"]
        hdr = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))["accepts"]
        return body, hdr

    def test_paid_tool_advertises_stellar_last(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = client.post("/tools/pre_trade_check/call", json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        body, hdr = self._entries(r)
        assert body[0]["network"].startswith("eip155:")
        st = [e for e in body if e["network"] == "stellar:testnet"]
        assert len(st) == 1 and st[0] is body[-1]
        assert st[0]["payTo"] == GW and st[0]["scheme"] == "exact"
        assert st[0]["asset"] == sx.USDC_SAC["stellar:testnet"]
        assert st[0]["maxAmountRequired"] == st[0]["amount"]
        assert st[0]["extra"] == {"areFeesSponsored": True}
        assert [e["network"] for e in hdr] == [e["network"] for e in body]

    def test_stellar_entry_builds_header_when_base_is_off(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "")
        r = client.post("/tools/pre_trade_check/call", json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        body, hdr = self._entries(r)
        assert [e["network"] for e in body] == ["stellar:testnet"]
        assert hdr[0]["network"] == "stellar:testnet"

    def test_free_tool_gets_no_stellar_entry(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = client.post("/tools/token_price/call", json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        assert not [e for e in r.json()["accepts"] if e["network"].startswith("stellar:")]

    def test_flag_off_advertises_nothing(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        monkeypatch.setattr(settings, "STELLAR_STANDARD_CLIENTS", False)
        r = client.post("/tools/pre_trade_check/call", json={"parameters": {"symbol": "ETH"}})
        assert not [e for e in r.json()["accepts"] if e["network"].startswith("stellar:")]
