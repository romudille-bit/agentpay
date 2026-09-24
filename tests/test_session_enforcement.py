"""Server-side session cap (AGE-207) under the session_enforcement fixture.

The cap is bound to the address that paid for session_create. On Stacks and
Base a priced call is reserved before anything settles and refused past the
cap with nothing charged; a settle that charged nothing gives the
reservation back; an uncertain settle keeps it. Stellar is recorded only.
"""

import base64
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

import gateway.base as base_pay
import gateway.routes.tools as rt
import gateway.stacks as stacks_pay
from gateway.config import settings
from gateway.services import sessions
from tests.test_stacks_gateway import (
    GATEWAY_ADDR,
    HIRO,
    PAYER,
    PAYMENT_ID,
    _header_for,
    _signed_tx,
)

STACKS_NET = "stacks-testnet"
STACKS_PAYER = PAYER.address("testnet")


async def _open(payer, max_spend="0.03", network=STACKS_NET, **kw):
    return await sessions.open_session(payer=payer, network=network, max_spend=max_spend,
                                       label=kw.get("label"), payment_id="pay-1",
                                       ttl_seconds=kw.get("ttl_seconds"))


# ── service ─────────────────────────────────────────────────────────────────


class TestService:
    async def test_unmetered_payer(self, session_enforcement):
        assert await sessions.reserve("SPNOBODY", "0.01", STACKS_NET) is None

    async def test_reserve_until_cap_then_refuse(self, session_enforcement):
        s = await _open("SPA", "0.03")
        holds = [await sessions.reserve("SPA", "0.01", STACKS_NET) for _ in range(3)]
        assert all(h.refused is None and h.session_id == s["session_id"] for h in holds)
        assert holds[-1].remaining == "0.00"
        over = await sessions.reserve("SPA", "0.01", STACKS_NET)
        assert over.refused == sessions.REFUSED_OVER_CAP
        assert over.spent == "0.03"

    async def test_second_create_returns_existing(self, session_enforcement):
        first = await _open("SPA", "0.03")
        second = await _open("SPA", "5")
        assert second["reused"] is True
        assert second["session_id"] == first["session_id"]
        assert second["max_spend"] == "0.03"

    async def test_release_reopens(self, session_enforcement):
        await _open("SPA", "0.01")
        h = await sessions.reserve("SPA", "0.01", STACKS_NET)
        assert (await sessions.reserve("SPA", "0.01", STACKS_NET)).refused
        await sessions.release(h)
        assert (await sessions.reserve("SPA", "0.01", STACKS_NET)).refused is None

    async def test_store_outage_refuses_enforced_rails_only(self, session_enforcement):
        store, _ = session_enforcement
        await _open("SPA", "1")
        store.rpc_down = True
        assert (await sessions.reserve("SPA", "0.01", STACKS_NET)).refused == sessions.REFUSED_UNAVAILABLE
        assert (await sessions.reserve("0xA", "0.01", "base-mainnet")).refused == sessions.REFUSED_UNAVAILABLE
        assert (await sessions.reserve("GA", "0.01", "stellar-mainnet")).refused is None

    async def test_expired_session_frees_the_payer(self, session_enforcement):
        store, _ = session_enforcement
        first = await _open("SPA", "1", ttl_seconds=60)
        store.now_offset_s = 120
        assert await sessions.reserve("SPA", "0.01", STACKS_NET) is None
        assert store.rows[first["session_id"]]["status"] == "expired"
        again = await _open("SPA", "2")
        assert again["reused"] is False and again["session_id"] != first["session_id"]

    async def test_ttl_is_bounded(self, session_enforcement):
        assert sessions.clamp_ttl(None) == settings.SESSION_DEFAULT_TTL_S
        assert sessions.clamp_ttl(5) == 60
        assert sessions.clamp_ttl(10 ** 9) == settings.SESSION_MAX_TTL_S

    async def test_off_is_a_no_op(self, session_enforcement, mock_settings, monkeypatch):
        monkeypatch.setattr(mock_settings, "SESSION_ENFORCEMENT", False)
        assert await _open("SPA") is None
        assert await sessions.reserve("SPA", "0.01", STACKS_NET) is None


# ── Stacks route ────────────────────────────────────────────────────────────


@pytest.fixture
def stacks_env(monkeypatch):
    monkeypatch.setattr(settings, "STACKS_ENABLED", True)
    monkeypatch.setattr(settings, "STACKS_NETWORK", "testnet")
    monkeypatch.setattr(settings, "STACKS_GATEWAY_ADDRESS", GATEWAY_ADDR)
    monkeypatch.setattr(settings, "STACKS_HIRO_API", "")
    monkeypatch.setattr(settings, "STACKS_FACILITATOR_URL", "")
    monkeypatch.setattr(settings, "STACKS_FIXED_BTC_USD", "100000")
    monkeypatch.setattr(settings, "STACKS_CONFIRM_POLL_S", 0.01)
    monkeypatch.setattr(settings, "STACKS_CONFIRM_MAX_POLLS", 3)
    monkeypatch.setattr(settings, "STACKS_SETTLE_TIMEOUT_S", 5.0)
    stacks_pay._used_stacks_txids.clear()
    yield
    stacks_pay._used_stacks_txids.clear()


def _hiro(router, *statuses, broadcast=True):
    if broadcast:
        bc = router.post(f"{HIRO}/v2/transactions").mock(
            return_value=httpx.Response(200, json="0" * 64))
    else:
        bc = router.post(f"{HIRO}/v2/transactions").mock(
            return_value=httpx.Response(400, json={"error": "transaction rejected",
                                                  "reason": "BadNonce",
                                                  "reason_data": {"expected": 9, "actual": 4}}))
    responses = [httpx.Response(200, json={"tx_status": s}) for s in statuses] or \
                [httpx.Response(200, json={"tx_status": "success"})]
    router.get(url__regex=rf"{HIRO}/extended/v1/tx/0x[0-9a-f]{{64}}$").mock(
        side_effect=responses + [responses[-1]] * 10)
    return bc


class _Tool:
    name = "verified_route"
    price_usdc = "0.001"
    developer_address = ""
    description = "test tool"


class TestStacksRoute:
    @pytest.fixture(autouse=True)
    def _route(self, monkeypatch, session_enforcement, stacks_env):
        self.store, self.router = session_enforcement

        async def _lookup(pid):  # any id: one challenge per call
            return {"payment_id": pid, "tool_name": "verified_route", "amount_usdc": "0.001",
                    "expires_at": 9999999999.0, "stacks_sats": 1, "stacks_rate": "100000"}

        monkeypatch.setattr(rt, "_lookup_challenge", _lookup)

    async def _settle(self, payment_id, nonce=4):
        tx = _signed_tx(amount_sats=1, payment_id=payment_id, nonce=nonce)
        header = _header_for(tx, payment_id=payment_id)
        res = await rt._settle_stacks_path(_Tool(), "verified_route", header,
                                           json.loads(base64.b64decode(header)))
        return res, stacks_pay.txid_of(tx)

    async def test_over_cap_refused_before_broadcast_and_challenge_kept(self, sb_semantics):
        s = await _open(STACKS_PAYER, "0.001")
        bc = _hiro(self.router)
        ok, _ = await self._settle("pid-1")
        assert isinstance(ok, dict) and ok["session"].session_id == s["session_id"]

        refused, _ = await self._settle("pid-2", nonce=5)
        body = json.loads(refused.body)
        assert refused.status_code == 402
        assert body["error_reason"] == sessions.REFUSED_OVER_CAP
        assert body["charged"] == "0" and body["remaining"] == "0.000"
        assert bc.call_count == 1
        assert "pid-2" not in sb_semantics.payment_ids  # 402 still redeemable

    async def test_rejected_broadcast_gives_reservation_back(self):
        await _open(STACKS_PAYER, "0.001")
        _hiro(self.router, broadcast=False)
        rejected, _ = await self._settle("pid-1")
        assert json.loads(rejected.body)["payment_status"] == "rejected"
        assert Decimal(self.store.rows[next(iter(self.store.rows))]["spent"]) == 0
        _hiro(self.router)
        ok, _ = await self._settle("pid-2", nonce=5)
        assert isinstance(ok, dict)

    async def test_uncertain_keeps_reservation_until_dropped(self, sb_semantics):
        s = await _open(STACKS_PAYER, "0.001")
        _hiro(self.router, "pending", "pending", "pending")
        unc, txid = await self._settle("pid-1")
        assert unc.status_code == 503
        assert sb_semantics.logs[txid]["session_id"] == s["session_id"]
        assert Decimal(self.store.rows[s["session_id"]]["spent"]) == Decimal("0.001")

        # redeem finds the tx aborted → row rejected, reservation released
        self.router.get(url__regex=rf"{HIRO}/extended/v1/tx/0x[0-9a-f]{{64}}$").mock(
            return_value=httpx.Response(200, json={"tx_status": "abort_by_response"}))
        again, _ = await self._settle("pid-1")
        assert json.loads(again.body)["error_reason"] == "abort_by_response"
        assert Decimal(self.store.rows[s["session_id"]]["spent"]) == 0

    async def test_no_session_is_unmetered(self):
        bc = _hiro(self.router)
        ok, _ = await self._settle("pid-1")
        assert isinstance(ok, dict) and ok["session"] is None
        assert bc.call_count == 1


# ── Base route ───────────────────────────────────────────────────────────────


def _mode_a(payer="0x" + "b" * 40):
    return base64.b64encode(json.dumps({
        "x402Version": 2, "scheme": "exact", "network": "eip155:8453",
        "payload": {"signature": "0x" + "c" * 130,
                    "authorization": {"from": payer, "to": "0x" + "d" * 40,
                                      "value": "1000", "nonce": "0x" + "e" * 64}},
    }).encode()).decode()


class TestBaseRoute:
    @pytest.fixture(autouse=True)
    def _base(self, monkeypatch, session_enforcement):
        self.store, _ = session_enforcement
        monkeypatch.setattr(settings, "BASE_GATEWAY_ADDRESS", "0x" + "d" * 40)
        monkeypatch.setattr(settings, "BASE_NETWORK", "eip155:8453")
        self.settles = []
        self.outcome = {"success": True, "tx_hash": "0x" + "1" * 64,
                        "payer": "0x" + "b" * 40, "network": "eip155:8453", "reason": "ok"}

        async def settle(header, req, **kw):
            self.settles.append(header)
            return dict(self.outcome)

        monkeypatch.setattr(base_pay, "settle_base_payment", settle)

    async def _call(self):
        return await rt._settle_base_path(_Tool(), "verified_route", _mode_a(),
                                          "https://agentpay.tools/tools/verified_route/call")

    async def test_over_cap_refused_without_settling(self):
        await _open("0x" + "b" * 40, "0.001", network="base-mainnet")
        assert isinstance(await self._call(), dict)
        refused = await self._call()
        assert refused.status_code == 402
        assert json.loads(refused.body)["reason"] == sessions.REFUSED_OVER_CAP
        assert len(self.settles) == 1

    async def test_failed_settle_releases(self):
        s = await _open("0x" + "b" * 40, "0.001", network="base-mainnet")
        self.outcome = {**self.outcome, "success": False, "reason": "insufficient_funds"}
        assert (await self._call()).status_code == 402
        assert Decimal(self.store.rows[s["session_id"]]["spent"]) == 0


# ── terminal write + response ───────────────────────────────────────────────


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                           headers={"user-agent": "test"})


class TestExecuteAndLog:
    async def test_payment_done_row_and_response_carry_the_session(
            self, session_enforcement, sb_semantics, monkeypatch):
        s = await _open(STACKS_PAYER, "0.05")
        hold = await sessions.reserve(STACKS_PAYER, "0.001", STACKS_NET)

        async def run(*a, **k):
            return {"ok": True}

        monkeypatch.setattr(rt, "_run_tool", run)
        auth = {"authorized": True, "tx_hash": "0xab", "payer": STACKS_PAYER,
                "network": STACKS_NET, "session": hold}
        body = rt.ToolCallRequest(parameters={"q": 1})
        resp = await rt._execute_and_log(_Tool(), "verified_route", "verified_route", body,
                                         _request(), auth, STACKS_PAYER, "0xab", True)
        row = sb_semantics.logs["0xab"]
        assert row["state"] == "payment_done" and row["session_id"] == s["session_id"]
        assert resp["session"]["remaining"] == "0.049"
        assert resp["session"]["url"].endswith(f"/v1/session/{s['session_id']}")

    async def test_session_create_via_tools_route_binds_the_payer(
            self, session_enforcement, sb_semantics):
        store, router = session_enforcement
        import registry
        tool = registry.get_tool("session_create")
        # the registry entry proxies to /v1/session/create; unpaid → 402 → real-API fallback
        router.post(tool.endpoint).mock(return_value=httpx.Response(402, json={}))
        auth = {"authorized": True, "tx_hash": "0xcd", "payer": STACKS_PAYER,
                "network": STACKS_NET, "session": None}
        body = rt.ToolCallRequest(parameters={"max_spend": "0.07", "label": "bot"})
        resp = await rt._execute_and_log(tool, "session_create", "session_create", body,
                                         _request(), auth, STACKS_PAYER, "0xcd", True)
        result = resp["result"]
        assert result["enforced"] is True and result["reused"] is False
        row = store.rows[result["session_id"]]
        assert row["payer"] == STACKS_PAYER and row["max_spend"] == "0.07"
        assert (await sessions.reserve(STACKS_PAYER, "0.01", STACKS_NET)).session_id == result["session_id"]


# ── HTTP: /v1/session/create + GET /v1/session/{id} ─────────────────────────


class TestHttp:
    @pytest.fixture(autouse=True)
    def _stellar(self, monkeypatch, session_enforcement):
        import gateway.x402 as x402_mod
        self.store, _ = session_enforcement

        async def verify(**kw):
            return {"verified": True, "reason": "ok"}

        async def split(**kw):
            return {"success": True}

        monkeypatch.setattr(x402_mod, "verify_payment", verify)
        monkeypatch.setattr(x402_mod, "split_payment", split)

    def _create(self, client, tx, max_spend="0.20"):
        agent = "GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGEN"
        ch = client.post("/v1/session/create", json={"agent_address": agent}).json()
        return client.post("/v1/session/create",
                           json={"agent_address": agent, "max_spend": max_spend},
                           headers={"X-Payment": f"tx_hash={tx},from={agent},id={ch['payment_id']}"})

    def test_create_persists_and_second_create_reuses(self, client):
        first = self._create(client, "tx1")
        assert first.status_code == 200, first.text
        j = first.json()
        assert j["enforced"] is True and j["reused"] is False and j["expires_at"]
        assert j["session_id"] in self.store.rows

        second = self._create(client, "tx2", max_spend="9").json()
        assert second["reused"] is True and second["session_id"] == j["session_id"]
        assert self.store.rows[j["session_id"]]["max_spend"] == "0.20"

    def test_get_session(self, client, sb_semantics):
        j = self._create(client, "tx1").json()
        sid = j["session_id"]
        sb_semantics.logs["0x77"] = {"payment_id": "0x77", "tool_name": "pre_trade_check",
                                     "network": STACKS_NET, "amount_usdc": "0.01",
                                     "state": "payment_done", "tx_hash": "0x77",
                                     "created_at": "2026-09-24T00:00:00Z", "session_id": sid}
        r = client.get(f"/v1/session/{sid}")
        assert r.status_code == 200
        view = r.json()
        assert view["remaining"] == "0.20" and view["status"] == "active"
        assert view["receipts"][0]["tx_hash"] == "0x77"
        assert view["enforced_rails"] == ["stacks", "base"]
        assert client.get("/v1/session/not-a-uuid").status_code == 404
        assert client.get("/v1/session/00000000-0000-0000-0000-000000000000").status_code == 404

    def test_get_404_when_off(self, client, mock_settings, monkeypatch):
        monkeypatch.setattr(mock_settings, "SESSION_ENFORCEMENT", False)
        assert client.get("/v1/session/00000000-0000-0000-0000-000000000000").status_code == 404
