"""
test_stacks_standard_clients.py — the sBTC option payable by standard Stacks
x402 clients (flag STACKS_STANDARD_CLIENTS), with the AgentPay SDK path
unchanged.

The two payment-signature headers in fixtures/stacks_standard_clients.json
were produced by the REAL published clients — x402-stacks 2.0.3
(wrapAxiosWithPayment) and @aibtc/mcp-server 1.71.0 (createApiClient) —
paying a flag-on mainnet 402 from this gateway, served locally. Signed with
throwaway keys, never broadcast. What each client does differently from our
SDK, and what these tests pin:

  x402-stacks  payload.transaction, memo = its own "x402:…" nonce,
               post-condition mode ALLOW (no post-conditions)
  AIBTC        payload.transaction with 0x prefix, memo = none,
               post-condition mode DENY + exact amount
  both         no top-level network or payment_id — they echo the accepts[]
               entry they chose as `accepted`, whose extra.payment_id we set

Regenerate: serve a flag-on mainnet 402 (Base + sBTC accepts) on localhost,
call it through each client with a throwaway key, capture the
payment-signature header on the retry.
"""

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

import gateway.stacks as stacks_pay
from agentpay._stacks_tx import (
    SBTC_CONTRACT_MAINNET,
    StacksKeypair,
    build_sbtc_transfer,
    sign_transaction,
    txid_of,
)
from gateway.config import settings
from gateway.services import supabase as sb

FX = json.loads((Path(__file__).parent / "fixtures" / "stacks_standard_clients.json").read_text())
GW = FX["gateway_address"]
PID = FX["payment_id"]
SATS = FX["amount_sats"]
HIRO = "https://api.hiro.so"
PAYER_KEY = "000000000000000000000000000000000000000000000000000000000000000101"
PID2 = "0b9c2f4e-1d3a-4e5f-9a7b-2c4d6e8f0a1b"   # a second live challenge, same price


@pytest.fixture(autouse=True)
def mainnet_settings(monkeypatch):
    monkeypatch.setattr(settings, "STACKS_ENABLED", True)
    monkeypatch.setattr(settings, "STACKS_NETWORK", "mainnet")
    monkeypatch.setattr(settings, "STACKS_GATEWAY_ADDRESS", GW)
    monkeypatch.setattr(settings, "STACKS_SBTC_CONTRACT", "")
    monkeypatch.setattr(settings, "STACKS_HIRO_API", "")
    monkeypatch.setattr(settings, "STACKS_FACILITATOR_URL", "")
    monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "100000")
    monkeypatch.setattr(settings, "STACKS_FEE_ESTIMATE", False)
    monkeypatch.setattr(settings, "STACKS_CONFIRM_POLL_S", 0.01)
    monkeypatch.setattr(settings, "STACKS_CONFIRM_MAX_POLLS", 3)
    monkeypatch.setattr(settings, "STACKS_STANDARD_CLIENTS", True)
    monkeypatch.setattr(sb, "sb_enabled", lambda: False)
    stacks_pay._used_stacks_txids.clear()
    yield
    stacks_pay._used_stacks_txids.clear()


def _payload(client):
    return json.loads(base64.b64decode(FX["headers"][client]))


async def _verify(header, *, standard_client, payment_id=PID, sats=SATS):
    return await stacks_pay.verify_stacks_payment(
        header, expected_amount_sats=sats, expected_recipient=GW,
        payment_id=payment_id, standard_client=standard_client,
    )


# ── payload helpers ──────────────────────────────────────────────────────────


class TestPayloadHelpers:
    @pytest.mark.parametrize("client", ["x402-stacks", "aibtc"])
    def test_real_clients_echo_our_payment_id(self, client):
        p = _payload(client)
        assert "payment_id" not in p and "network" not in p
        assert stacks_pay.payload_payment_id(p) == (PID, True)
        assert stacks_pay.payload_network(p) == "stacks:1"
        assert bytes.fromhex(stacks_pay.payload_signed_tx_hex(p))  # 0x stripped

    def test_sdk_shape_is_not_echoed(self):
        p = {"payment_id": "abc", "network": "stacks:1",
             "payload": {"signedTransaction": "00ff"}}
        assert stacks_pay.payload_payment_id(p) == ("abc", False)
        assert stacks_pay.payload_network(p) == "stacks:1"
        assert stacks_pay.payload_signed_tx_hex(p) == "00ff"


# ── verifier ─────────────────────────────────────────────────────────────────


class TestVerifyRealClients:
    async def test_x402_stacks_accepted_as_standard_client(self):
        auth = await _verify(FX["headers"]["x402-stacks"], standard_client=True)
        assert auth["authorized"], auth["reason"]
        assert auth["binding"] == "echoed_payment_id"
        assert auth["payer_protection"] == "none_allow_mode"
        assert auth["amount_sats"] == SATS

    async def test_aibtc_accepted_as_standard_client(self):
        auth = await _verify(FX["headers"]["aibtc"], standard_client=True)
        assert auth["authorized"], auth["reason"]
        assert auth["binding"] == "echoed_payment_id"
        assert auth["payer_protection"] == "deny_mode_exact_amount"

    async def test_both_refused_on_the_sdk_path(self):
        # standard_client=False is exactly today's rule set.
        a = await _verify(FX["headers"]["x402-stacks"], standard_client=False)
        b = await _verify(FX["headers"]["aibtc"], standard_client=False)
        assert (a["authorized"], a["reason"]) == (False, "memo_payment_id_mismatch")
        assert (b["authorized"], b["reason"]) == (False, "missing_memo_binding")

    async def test_underpay_still_refused_for_standard_clients(self):
        auth = await _verify(FX["headers"]["aibtc"], standard_client=True, sats=SATS * 10)
        assert not auth["authorized"] and auth["reason"].startswith("underpaid")

    async def test_wrong_recipient_still_refused(self):
        other = StacksKeypair.from_secret(PAYER_KEY).address("mainnet")
        auth = await stacks_pay.verify_stacks_payment(
            FX["headers"]["aibtc"], expected_amount_sats=SATS,
            expected_recipient=other, payment_id=PID, standard_client=True)
        assert (auth["authorized"], auth["reason"]) == (False, "wrong_recipient")

    async def test_memo_naming_another_challenge_refused_even_for_standard(self):
        # A tx the SDK signed for challenge A, echoed under challenge B.
        kp = StacksKeypair.from_secret(PAYER_KEY)
        other_pid = "11111111-2222-4333-8444-555555555555"
        tx = sign_transaction(build_sbtc_transfer(
            sender=kp, recipient=GW, amount_sats=SATS, payment_id=other_pid,
            nonce=1, fee_microstx=500, network="mainnet"), kp)
        header = base64.b64encode(json.dumps({
            "x402Version": 2, "accepted": {"network": "stacks:1",
                                           "extra": {"payment_id": PID}},
            "payload": {"transaction": tx.hex()}}).encode()).decode()
        auth = await _verify(header, standard_client=True)
        assert (auth["authorized"], auth["reason"]) == (False, "memo_payment_id_mismatch")

    async def test_sdk_tx_unchanged_binding_and_protection(self):
        kp = StacksKeypair.from_secret(PAYER_KEY)
        tx = sign_transaction(build_sbtc_transfer(
            sender=kp, recipient=GW, amount_sats=SATS, payment_id=PID,
            nonce=1, fee_microstx=500, network="mainnet"), kp)
        header = base64.b64encode(json.dumps({
            "x402Version": 2, "network": "stacks:1", "payment_id": PID,
            "payload": {"signedTransaction": tx.hex()}}).encode()).decode()
        auth = await _verify(header, standard_client=False)
        assert auth["authorized"]
        assert auth["binding"] == "memo"
        assert auth["payer_protection"] == "deny_mode_exact_amount"


class TestAllowModeOnlyOnCanonicalSbtc:
    """Allow mode is safe only because the real sBTC contract moves exactly
    `amount`. STACKS_SBTC_CONTRACT is overridable, so allow mode must be
    tied to the pinned official ids, not to whatever the override says."""

    def _allow_mode_header(self, monkeypatch, contract):
        kp = StacksKeypair.from_secret(PAYER_KEY)
        tx = sign_transaction(build_sbtc_transfer(
            sender=kp, recipient=GW, amount_sats=SATS, payment_id="n/a",
            nonce=1, fee_microstx=500, network="mainnet", contract=contract), kp)
        decoded = stacks_pay.decode_sbtc_transfer(tx)
        # The SDK lib only builds deny mode; present it as allow mode.
        monkeypatch.setattr(stacks_pay, "decode_sbtc_transfer",
                            lambda _b: {**decoded, "pc_mode": 0x01, "memo": None})
        return base64.b64encode(json.dumps({
            "x402Version": 2,
            "accepted": {"network": "stacks:1", "extra": {"payment_id": PID}},
            "payload": {"transaction": tx.hex()}}).encode()).decode()

    async def test_non_canonical_override_requires_deny_mode(self, monkeypatch):
        fake = "SP000000000000000000002Q6VF78.fake-sbtc-token"
        monkeypatch.setattr(settings, "STACKS_SBTC_CONTRACT", fake)
        header = self._allow_mode_header(monkeypatch, fake)
        auth = await _verify(header, standard_client=True)
        assert (auth["authorized"], auth["reason"]) == (False, "post_condition_mode_not_deny")

    async def test_canonical_contract_allows_allow_mode(self, monkeypatch):
        # Explicitly set to the real id: still accepted (the check is on the
        # pinned constant, and this override happens to equal it).
        monkeypatch.setattr(settings, "STACKS_SBTC_CONTRACT", SBTC_CONTRACT_MAINNET)
        header = self._allow_mode_header(monkeypatch, SBTC_CONTRACT_MAINNET)
        auth = await _verify(header, standard_client=True)
        assert auth["authorized"], auth["reason"]
        assert auth["payer_protection"] == "none_allow_mode"


# ── route glue (_settle_stacks_path) ─────────────────────────────────────────


class _Tool:
    name = "pre_trade_check"
    price_usdc = "0.01"


@pytest.fixture
def rt(monkeypatch):
    import gateway.routes.tools as rt
    challenge = {"payment_id": PID, "tool_name": "pre_trade_check",
                 "amount_usdc": "0.01", "expires_at": 9999999999.0,
                 "stacks_sats": SATS, "stacks_rate": "100000"}

    challenges = {PID: challenge, PID2: {**challenge, "payment_id": PID2}}

    async def _lookup(pid):
        return challenges.get(pid)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(rt, "_lookup_challenge", _lookup)
    monkeypatch.setattr(rt, "update_payment_log_state", _noop)
    monkeypatch.setattr(rt, "_record_rejected_attempt", _noop)
    monkeypatch.setattr(rt, "sb_enabled", lambda: False)
    return rt


def _hiro_ok():
    respx.post(f"{HIRO}/v2/transactions").mock(
        return_value=httpx.Response(200, json="0" * 64))
    respx.get(url__regex=rf"{HIRO}/extended/v1/tx/0x[0-9a-f]{{64}}$").mock(
        return_value=httpx.Response(200, json={"tx_status": "success"}))


class TestRouteGlue:
    @pytest.mark.parametrize("client,protection", [
        ("x402-stacks", "none_allow_mode"),
        ("aibtc", "deny_mode_exact_amount"),
    ])
    async def test_real_client_settles_with_flag_on(self, rt, client, protection):
        header = FX["headers"][client]
        with respx.mock:
            _hiro_ok()
            auth = await rt._settle_stacks_path(_Tool(), "pre_trade_check",
                                                header, _payload(client))
        assert isinstance(auth, dict) and auth["authorized"], getattr(auth, "body", auth)
        assert auth["binding"] == "echoed_payment_id"
        assert auth["payer_protection"] == protection
        tx_hex = stacks_pay.payload_signed_tx_hex(_payload(client))
        assert auth["tx_hash"] == txid_of(bytes.fromhex(tx_hex))

    @pytest.mark.parametrize("client", ["x402-stacks", "aibtc"])
    async def test_same_header_twice_settles_once(self, rt, client):
        header = FX["headers"][client]
        with respx.mock:
            _hiro_ok()
            first = await rt._settle_stacks_path(_Tool(), "pre_trade_check",
                                                 header, _payload(client))
            second = await rt._settle_stacks_path(_Tool(), "pre_trade_check",
                                                  header, _payload(client))
        assert isinstance(first, dict) and first["authorized"]
        assert second.status_code == 402
        body = json.loads(second.body)
        assert body["payment_status"] == "rejected"
        assert body["error_reason"] == "replay_attack", body   # the txid consume, not the payment_id store

    @pytest.mark.parametrize("client", ["x402-stacks", "aibtc"])
    async def test_same_tx_echoed_under_second_challenge_rejected(self, rt, client):
        # The binding for standard clients is the echoed id, so an attacker
        # (or a confused client) can point one signed tx at another live
        # challenge of the same price. The txid consume must stop it.
        header = FX["headers"][client]
        p2 = _payload(client)
        p2["accepted"] = {**p2["accepted"],
                          "extra": {**p2["accepted"]["extra"], "payment_id": PID2}}
        header2 = base64.b64encode(json.dumps(p2).encode()).decode()
        with respx.mock:
            _hiro_ok()
            first = await rt._settle_stacks_path(_Tool(), "pre_trade_check",
                                                 header, _payload(client))
            second = await rt._settle_stacks_path(_Tool(), "pre_trade_check",
                                                  header2, p2)
        assert isinstance(first, dict) and first["authorized"]
        assert second.status_code == 402
        body = json.loads(second.body)
        assert body["payment_status"] == "rejected"
        assert body["error_reason"] == "replay_attack", body   # the txid consume, not the payment_id store

    async def test_flag_off_rejects_echoed_id(self, rt, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_STANDARD_CLIENTS", False)
        resp = await rt._settle_stacks_path(_Tool(), "pre_trade_check",
                                            FX["headers"]["aibtc"], _payload("aibtc"))
        assert resp.status_code == 402
        assert json.loads(resp.body)["error_reason"] == "missing_payment_id"


# ── the 402 itself ───────────────────────────────────────────────────────────


def _paid_402(client):
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"coingecko").mock(
            return_value=httpx.Response(200, json={"bitcoin": {"usd": 100000}}))
        return client.post("/tools/pre_trade_check/call",
                           json={"parameters": {"symbol": "ETH"}})


class Test402Accepts:
    def test_flag_on_appends_sbtc_after_base(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = _paid_402(client)
        assert r.status_code == 402
        body = r.json()
        header = json.loads(base64.b64decode(r.headers["payment-required"]))
        for accepts in (body["accepts"], header["accepts"]):
            # Index 0 is what Bazaar/CDP read — it must stay Base.
            assert accepts[0]["network"].startswith("eip155:")
            st = accepts[1]
            assert st["network"] == "stacks:1"
            assert st["asset"] == "SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token"
            assert st["payTo"] == GW
            assert st["extra"]["payment_id"] == body["payment_id"]
            assert int(st["amount"]) == body["payment_options"]["stacks"]["amount_sats"]
            # Derived from the challenge's own remaining life.
            assert 0 < st["maxTimeoutSeconds"] <= 120

    def test_flag_off_accepts_is_base_only(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        monkeypatch.setattr(settings, "STACKS_STANDARD_CLIENTS", False)
        r = _paid_402(client)
        body = r.json()
        header = json.loads(base64.b64decode(r.headers["payment-required"]))
        assert [a["network"][:6] for a in body["accepts"]] == ["eip155"]
        assert [a["network"][:6] for a in header["accepts"]] == ["eip155"]
        assert "stacks" in body["payment_options"]   # SDK option untouched

    def test_dispatch_routes_accepted_network_to_stacks(self, client, monkeypatch):
        import gateway.routes.tools as rt
        seen = {}

        async def _fake(tool, tool_name, ps, payload, parameters=None):
            seen["network"] = stacks_pay.payload_network(payload)
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=402, content={"payment_status": "rejected"})
        monkeypatch.setattr(rt, "_settle_stacks_path", _fake)
        r = client.post("/tools/pre_trade_check/call",
                        json={"parameters": {"symbol": "ETH"}},
                        headers={"payment-signature": FX["headers"]["aibtc"]})
        assert seen.get("network") == "stacks:1", r.text


class TestAppendAcceptsToHeader:
    ENTRY = {"network": "stacks:1", "asset": "x.sbtc-token"}

    def test_undecodable_header_left_untouched(self):
        import gateway.routes.tools as rt
        bad = "this is not base64 json"
        assert rt._append_accepts_to_header(bad, self.ENTRY, "https://x/y", "d") == bad

    def test_existing_header_keeps_its_index_0(self):
        import gateway.routes.tools as rt
        base_hdr = base64.b64encode(json.dumps(
            {"x402Version": 2, "accepts": [{"network": "eip155:8453"}]}).encode()).decode()
        out = json.loads(base64.b64decode(
            rt._append_accepts_to_header(base_hdr, self.ENTRY, "https://x/y", "d")))
        assert [a["network"] for a in out["accepts"]] == ["eip155:8453", "stacks:1"]

    def test_no_header_builds_minimal_one(self):
        import gateway.routes.tools as rt
        out = json.loads(base64.b64decode(
            rt._append_accepts_to_header(None, self.ENTRY, "https://x/y", "d")))
        assert out["accepts"] == [self.ENTRY] and out["x402Version"] == 2
