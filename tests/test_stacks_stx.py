"""Native STX alongside sBTC for standard Stacks clients (AGE-208).

The 402 carries a second stacks:1 accepts[] entry (asset "STX", amount in
µSTX — the dialect x402-stacks, the AIBTC wallet and stx402.com share); a
client that echoes it pays with a plain STX token transfer, verified
against the µSTX quote stored on the challenge. sBTC stays first unless
the caller asks for STX.
"""

import base64
import json
from decimal import Decimal

import httpx
import pytest
import respx

import gateway.stacks as stacks_pay
from agentpay._stacks_tx import (
    StacksKeypair,
    build_sbtc_transfer,
    build_stx_transfer,
    sign_transaction,
    txid_of,
)
from gateway.config import settings
from gateway.services import supabase as sb

HIRO = "https://api.hiro.so"
PAYER_KEY = "000000000000000000000000000000000000000000000000000000000000000101"
KP = StacksKeypair.from_secret(PAYER_KEY)
GW = "SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C"
PID = "3f6f2b04-7a1e-4c1d-9d2a-active00test"
USTX = 23810          # $0.01 at 0.42 USD/STX, ceiled


@pytest.fixture(autouse=True)
def stx_settings(monkeypatch):
    monkeypatch.setattr(settings, "STACKS_ENABLED", True)
    monkeypatch.setattr(settings, "STACKS_NETWORK", "mainnet")
    monkeypatch.setattr(settings, "STACKS_GATEWAY_ADDRESS", GW)
    monkeypatch.setattr(settings, "STACKS_SBTC_CONTRACT", "")
    monkeypatch.setattr(settings, "STACKS_HIRO_API", "")
    monkeypatch.setattr(settings, "STACKS_FACILITATOR_URL", "")
    monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "100000")
    monkeypatch.setattr(settings, "STACKS_FIXED_STX_USD", "0.42")
    monkeypatch.setattr(settings, "STACKS_FEE_ESTIMATE", False)
    monkeypatch.setattr(settings, "STACKS_CONFIRM_POLL_S", 0.01)
    monkeypatch.setattr(settings, "STACKS_CONFIRM_MAX_POLLS", 3)
    monkeypatch.setattr(settings, "STACKS_STANDARD_CLIENTS", True)
    monkeypatch.setattr(settings, "STACKS_STX", True)
    monkeypatch.setattr(sb, "sb_enabled", lambda: False)
    stacks_pay._used_stacks_txids.clear()
    stacks_pay._rate_cache.update({"rate": None, "at": 0.0})
    stacks_pay._stx_rate_cache.update({"rate": None, "at": 0.0})
    yield
    stacks_pay._used_stacks_txids.clear()
    stacks_pay._stx_rate_cache.update({"rate": None, "at": 0.0})


def _stx_tx(amount=USTX, memo=b"", recipient=GW, nonce=3, network="mainnet"):
    return sign_transaction(build_stx_transfer(
        sender=KP, recipient=recipient, amount_ustx=amount, memo=memo,
        nonce=nonce, fee_microstx=180, network=network), KP)


def _header(tx: bytes, asset="STX", payment_id=PID, echo=True) -> str:
    payload = {"x402Version": 2, "payload": {"transaction": "0x" + tx.hex()}}
    if echo:
        payload["accepted"] = {"scheme": "exact", "network": "stacks:1", "asset": asset,
                               "payTo": GW, "extra": {"payment_id": payment_id}}
    else:
        payload.update({"network": "stacks:1", "payment_id": payment_id})
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def _verify(header, *, asset="stx", ustx=USTX, standard_client=True, recipient=GW):
    return await stacks_pay.verify_stacks_payment(
        header, expected_amount_sats=12, expected_recipient=recipient,
        payment_id=PID, standard_client=standard_client,
        asset=asset, expected_amount_ustx=ustx)


# ── wire ─────────────────────────────────────────────────────────────────────


class TestDecode:
    def test_token_transfer_fields(self):
        tx = _stx_tx(memo=b"x402:abc")
        d = stacks_pay.decode_stacks_transfer(tx)
        assert d["payload_type"] == "token_transfer"
        assert d["sender"] == KP.address("mainnet") == d["arg_sender"]
        assert d["arg_recipient"] == GW and d["amount"] == USTX
        assert d["memo"] == b"x402:abc"          # zero padding stripped
        assert d["post_conditions"] == [] and d["contract_id"] == ""

    def test_sbtc_decoder_refuses_a_token_transfer(self):
        with pytest.raises(ValueError):
            stacks_pay.decode_sbtc_transfer(_stx_tx())

    def test_contract_call_still_decodes(self):
        tx = sign_transaction(build_sbtc_transfer(
            sender=KP, recipient=GW, amount_sats=12, payment_id=PID,
            nonce=1, fee_microstx=500, network="mainnet"), KP)
        d = stacks_pay.decode_stacks_transfer(tx)
        assert d["payload_type"] == "contract_call" and d["function"] == "transfer"


class TestQuote:
    def test_ustx_rounds_up(self):
        assert stacks_pay.ustx_from_usd(Decimal("0.01"), Decimal("0.42")) == 23810
        assert stacks_pay.ustx_from_usd(Decimal("0.01"), Decimal("1")) == 10000
        assert stacks_pay.ustx_from_usd(Decimal("0.000000001"), Decimal("1")) == 1

    async def test_fixed_rate_fallback(self):
        with respx.mock(assert_all_called=False) as m:   # own router: other modules leave global routes
            m.get(url__regex=r"coingecko").mock(return_value=httpx.Response(500))
            q = await stacks_pay.stacks_stx_quote("0.01")
        assert q == (USTX, Decimal("0.42"))

    async def test_one_fetch_feeds_both_rates(self):
        with respx.mock(assert_all_called=False) as m:
            route = m.get(url__regex=r"coingecko").mock(return_value=httpx.Response(
                200, json={"bitcoin": {"usd": 100000}, "blockstack": {"usd": 0.5}}))
            assert (await stacks_pay.stacks_quote("0.01"))[1] == Decimal("100000")
            assert (await stacks_pay.stacks_stx_quote("0.01")) == (20000, Decimal("0.5"))
        assert route.call_count == 1

    def test_accepts_entry_shape(self):
        e = stacks_pay.stacks_accepts_entry_stx((USTX, Decimal("0.42")), "0.01", PID, 90)
        assert (e["asset"], e["amount"], e["network"], e["payTo"]) == ("STX", str(USTX), "stacks:1", GW)
        assert e["extra"] == {"payment_id": PID, "tokenType": "STX",
                              "amount_usdc": "0.01", "stx_usd_rate": "0.42"}

    def test_offerable_needs_both_flags(self, monkeypatch):
        assert stacks_pay.stacks_stx_offerable("0.01")
        assert not stacks_pay.stacks_stx_offerable("0")
        monkeypatch.setattr(settings, "STACKS_STX", False)
        assert not stacks_pay.stacks_stx_offerable("0.01")


# ── verify ───────────────────────────────────────────────────────────────────


class TestVerify:
    @pytest.mark.parametrize("memo", [b"", b"x402:8kQ1z_nonce"])   # AIBTC, x402-stacks
    async def test_standard_client_transfer_accepted(self, memo):
        tx = _stx_tx(memo=memo)
        auth = await _verify(_header(tx))
        assert auth["authorized"], auth["reason"]
        assert auth["asset"] == "stx" and auth["amount_ustx"] == USTX and auth["amount_sats"] == 0
        assert auth["binding"] == "echoed_payment_id"
        assert auth["payer_protection"] == "fixed_amount_transfer"
        assert auth["txid"] == txid_of(tx) and auth["sender"] == KP.address("mainnet")

    async def test_memo_binding_for_non_standard(self):
        tx = _stx_tx(memo=PID.encode()[:34])
        auth = await _verify(_header(tx, echo=False), standard_client=False)
        assert auth["authorized"] and auth["binding"] == "memo"

    async def test_memo_naming_another_challenge_refused(self):
        tx = _stx_tx(memo=b"0b9c2f4e-1d3a-4e5f-9a7b-2c4d6e8f0a1b"[:34])
        assert (await _verify(_header(tx)))["reason"] == "memo_payment_id_mismatch"

    async def test_underpaid_and_tolerance(self):
        assert (await _verify(_header(_stx_tx(amount=USTX - 1000))))["reason"].startswith("underpaid")
        assert (await _verify(_header(_stx_tx(amount=USTX - 100))))["authorized"]   # within 2%
        # small quotes: exact amount required
        assert (await _verify(_header(_stx_tx(amount=499)), ustx=500))["reason"].startswith("underpaid")

    async def test_wrong_recipient(self):
        other = KP.address("mainnet")
        assert (await _verify(_header(_stx_tx(recipient=other))))["reason"] == "wrong_recipient"

    async def test_wrong_network(self):
        assert (await _verify(_header(_stx_tx(network="testnet"))))["reason"] == "wrong_network"

    async def test_sbtc_transfer_under_the_stx_entry_refused(self):
        tx = sign_transaction(build_sbtc_transfer(
            sender=KP, recipient=GW, amount_sats=12, payment_id=PID,
            nonce=1, fee_microstx=500, network="mainnet"), KP)
        assert (await _verify(_header(tx)))["reason"] == "not_an_stx_transfer"

    async def test_stx_transfer_under_the_sbtc_entry_refused(self):
        auth = await _verify(_header(_stx_tx(), asset="SM3V.sbtc-token"), asset="sbtc")
        assert not auth["authorized"] and auth["reason"].startswith("malformed_stacks_tx")

    async def test_overpaid_is_flagged_not_refused(self):
        auth = await _verify(_header(_stx_tx(amount=USTX * 3)))
        assert auth["authorized"] and auth["overpaid"]


class TestPayloadAsset:
    def test_asset_detection(self):
        assert stacks_pay.payload_asset({"accepted": {"asset": "STX"}}) == "stx"
        assert stacks_pay.payload_asset({"accepted": {"asset": "stx"}}) == "stx"
        assert stacks_pay.payload_asset({"accepted": {"asset": "stacks:1/native"}}) == "stx"
        assert stacks_pay.payload_asset({"accepted": {"asset": "SM3V.sbtc-token"}}) == "sbtc"
        assert stacks_pay.payload_asset({}) == "sbtc"


# ── route ────────────────────────────────────────────────────────────────────


class _Tool:
    name = "pre_trade_check"
    price_usdc = "0.01"
    developer_address = ""
    description = "test tool"


@pytest.fixture
def rt(monkeypatch):
    import gateway.routes.tools as rt
    challenges = {PID: {"payment_id": PID, "tool_name": "pre_trade_check", "amount_usdc": "0.01",
                        "expires_at": 9999999999.0, "stacks_sats": 12, "stacks_rate": "100000",
                        "stacks_ustx": USTX, "stx_usd_rate": "0.42"}}

    async def _lookup(pid):
        return challenges.get(pid)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(rt, "_lookup_challenge", _lookup)
    monkeypatch.setattr(rt, "update_payment_log_state", _noop)
    monkeypatch.setattr(rt, "_record_rejected_attempt", _noop)
    monkeypatch.setattr(rt, "sb_enabled", lambda: False)
    rt._challenges = challenges
    return rt


def _hiro_ok():
    respx.post(f"{HIRO}/v2/transactions").mock(return_value=httpx.Response(200, json="0" * 64))
    respx.get(url__regex=rf"{HIRO}/extended/v1/tx/0x[0-9a-f]{{64}}$").mock(
        return_value=httpx.Response(200, json={"tx_status": "success"}))


class TestRoute:
    async def test_stx_settles_against_the_stored_quote(self, rt):
        tx = _stx_tx()
        header = _header(tx)
        with respx.mock:
            _hiro_ok()
            auth = await rt._settle_stacks_path(_Tool(), "pre_trade_check", header,
                                                json.loads(base64.b64decode(header)))
        assert isinstance(auth, dict) and auth["authorized"], getattr(auth, "body", auth)
        assert auth["asset"] == "stx" and auth["amount_ustx"] == USTX
        assert auth["stx_usd_rate"] == "0.42" and auth["btc_usd_rate"] == ""
        assert auth["tx_hash"] == txid_of(tx)

    async def test_requotes_when_the_challenge_has_no_stx_quote(self, rt):
        rt._challenges[PID].pop("stacks_ustx"); rt._challenges[PID].pop("stx_usd_rate")
        header = _header(_stx_tx())
        with respx.mock:
            respx.get(url__regex=r"coingecko").mock(return_value=httpx.Response(500))
            _hiro_ok()
            auth = await rt._settle_stacks_path(_Tool(), "pre_trade_check", header,
                                                json.loads(base64.b64decode(header)))
        assert isinstance(auth, dict) and auth["authorized"] and auth["stx_usd_rate"] == "0.42"

    async def test_flag_off_rejects_stx_before_verifying(self, rt, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_STX", False)
        header = _header(_stx_tx())
        with respx.mock:
            bc = respx.post(f"{HIRO}/v2/transactions")
            resp = await rt._settle_stacks_path(_Tool(), "pre_trade_check", header,
                                                json.loads(base64.b64decode(header)))
        assert resp.status_code == 402
        assert json.loads(resp.body)["error_reason"] == "stx_not_accepted"
        assert bc.call_count == 0

    async def test_receipt_carries_the_stx_fields(self, rt, monkeypatch):
        from types import SimpleNamespace
        import gateway.routes.tools as rtm

        async def run(*a, **k):
            return {"ok": True}
        monkeypatch.setattr(rtm, "_run_tool", run)
        monkeypatch.setattr(rtm, "insert_pending_payment_log", lambda *a, **k: None)
        auth = {"authorized": True, "tx_hash": "0xab", "payer": KP.address("mainnet"),
                "network": "stacks-mainnet", "asset": "stx", "amount_ustx": USTX,
                "stx_usd_rate": "0.42", "binding": "echoed_payment_id",
                "payer_protection": "fixed_amount_transfer", "session": None}
        req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"user-agent": "t"})
        resp = await rtm._execute_and_log(_Tool(), "pre_trade_check", "pre_trade_check",
                                          rtm.ToolCallRequest(parameters={}), req,
                                          auth, KP.address("mainnet"), "0xab", True)
        pay = resp["payment"]
        assert (pay["asset"], pay["amount_ustx"], pay["stx_usd_rate"]) == ("STX", USTX, "0.42")
        assert pay["payer_protection"] == "fixed_amount_transfer"


# ── the 402 ──────────────────────────────────────────────────────────────────


def _paid_402(client, path="/tools/pre_trade_check/call", headers=None, stx_rate=0.42):
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"coingecko").mock(return_value=httpx.Response(
            200, json={"bitcoin": {"usd": 100000}, "blockstack": {"usd": stx_rate}}))
        return client.post(path, json={"parameters": {"symbol": "ETH"}}, headers=headers or {})


class Test402:
    @pytest.fixture(autouse=True)
    def _base(self, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

    @staticmethod
    def _assets(r):
        body = r.json()
        header = json.loads(base64.b64decode(r.headers["payment-required"]))
        b = [a.get("asset") for a in body["accepts"]]
        h = [a.get("asset") for a in header["accepts"]]
        assert b == h
        return body, b

    def test_sbtc_first_then_stx(self, client):
        r = _paid_402(client)
        assert r.status_code == 402
        body, assets = self._assets(r)
        assert assets[1:] == ["SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token", "STX"]
        stx = body["accepts"][2]
        assert stx["network"] == "stacks:1" and stx["payTo"] == GW
        assert stx["extra"]["payment_id"] == body["payment_id"]
        assert int(stx["amount"]) == USTX
        assert body["payment_options"]["stacks"]["stx"] == {"amount_ustx": USTX, "stx_usd_rate": "0.42"}

    def test_query_puts_stx_first(self, client):
        _, assets = self._assets(_paid_402(client, path="/tools/pre_trade_check/call?asset=stx"))
        assert assets[1:] == ["STX", "SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token"]

    def test_header_puts_stx_first(self, client):
        _, assets = self._assets(_paid_402(client, headers={"X-Pay-Asset": "STX"}))
        assert assets[1] == "STX"

    def test_flag_off_no_stx_entry(self, client, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_STX", False)
        body, assets = self._assets(_paid_402(client))
        assert "STX" not in assets and "stx" not in body["payment_options"]["stacks"]

    def test_no_stx_rate_no_stx_entry(self, client, monkeypatch):
        monkeypatch.setattr(settings, "STACKS_FIXED_STX_USD", "")
        with respx.mock(assert_all_called=False) as m:
            m.get(url__regex=r"coingecko").mock(return_value=httpx.Response(
                200, json={"bitcoin": {"usd": 100000}}))
            r = client.post("/tools/pre_trade_check/call", json={"parameters": {"symbol": "ETH"}})
        _, assets = self._assets(r)
        assert "STX" not in assets and len(assets) == 2
