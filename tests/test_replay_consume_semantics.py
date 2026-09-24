"""Replay/consume contract on all three rails under Supabase-*enabled*
semantics (the `sb_semantics` fixture): a consumed proof is refused
across a gateway restart, an outage of the durable store fails closed
without stranding the proof, and a payment_id can only be spent once.
"""

import asyncio
import base64
import json

import httpx
import pytest
import respx

import gateway.base as base_mod
import gateway.stacks as stacks_pay
import gateway.x402 as x402_mod
from gateway.config import settings
from gateway.x402 import issue_payment_challenge, verify_and_fulfill
from tests.test_base import (
    VALID_PAYER,
    VALID_TX_HASH,
    _mode_b_signature_header,
    _payment_requirements,
)
from tests.test_stacks_gateway import (
    GATEWAY_ADDR,
    HIRO,
    PAYMENT_ID,
    _header_for,
    _hiro_broadcast_ok,
    _hiro_status,
    _signed_tx,
)

AGENT = "GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGEN"


# ── Stellar: verify_and_fulfill ──────────────────────────────────────────────


@pytest.fixture
def stellar_ok(monkeypatch):
    calls = {"verify": 0, "split": 0}

    async def verify(**kw):
        calls["verify"] += 1
        return {"verified": True, "reason": "ok"}

    async def split(**kw):
        calls["split"] += 1
        return {"success": True}

    monkeypatch.setattr(x402_mod, "verify_payment", verify)
    monkeypatch.setattr(x402_mod, "split_payment", split)
    return calls


def _challenge():
    return issue_payment_challenge(
        tool_name="token_price", price_usdc="0.001",
        developer_address="GDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDE",
        request_data={"parameters": {"symbol": "ETH"}},
    )


def _proof(payment_id, tx_hash="abc123def456"):
    return f"tx_hash={tx_hash},from={AGENT},id={payment_id}"


async def _fulfil(payment_id, tx_hash="abc123def456"):
    return await verify_and_fulfill(
        expected_tools=("token_price",), expected_price_usdc="0.001",
        payment_header=_proof(payment_id, tx_hash), agent_address=AGENT,
    )


async def _drain():
    for _ in range(5):  # let the create_task'd split run
        await asyncio.sleep(0)


class TestStellar:
    async def test_tx_hash_refused_across_restart(self, sb_semantics, stellar_ok):
        first = await _fulfil(_challenge().payment_id)
        assert first["authorized"] is True

        x402_mod._completed_payments.clear()  # process restart
        again = await _fulfil(_challenge().payment_id)  # fresh challenge, same tx
        assert again["authorized"] is False
        assert "replay" in again["reason"]
        await _drain()
        assert stellar_ok["split"] == 1

    async def test_outage_fails_closed_then_retry_succeeds(self, sb_semantics, stellar_ok):
        pid = _challenge().payment_id
        sb_semantics.outage = True
        blocked = await _fulfil(pid)
        assert blocked["authorized"] is False
        assert blocked["reason"].startswith("replay_check_unavailable")
        assert "abc123def456" not in x402_mod._completed_payments
        assert stellar_ok["split"] == 0

        sb_semantics.outage = False
        assert (await _fulfil(pid))["authorized"] is True

    async def test_half_consume_is_rolled_back(self, sb_semantics, stellar_ok):
        pid = _challenge().payment_id
        sb_semantics.outage_names.add("record_payment_id")
        blocked = await _fulfil(pid)
        assert blocked["authorized"] is False
        assert [c for c in sb_semantics.calls if c[0] == "unrecord_tx_hash"]
        assert not sb_semantics.tx_hashes

        sb_semantics.outage_names.clear()
        assert (await _fulfil(pid))["authorized"] is True

    async def test_concurrent_same_tx_fulfils_once(self, sb_semantics, stellar_ok):
        sb_semantics.yield_before_write = True
        pids = [_challenge().payment_id for _ in range(3)]
        results = await asyncio.gather(*(_fulfil(p) for p in pids))
        assert sum(r["authorized"] for r in results) == 1
        await _drain()
        assert stellar_ok["split"] == 1


# ── Base: settle_base_payment (Mode B) ───────────────────────────────────────


@pytest.fixture
def base_ok(monkeypatch):
    calls = {"verify": 0}

    async def verify(**kw):
        calls["verify"] += 1
        return {"success": True, "tx_hash": kw["tx_hash"], "payer": kw["payer"],
                "network": "", "reason": "ok"}

    monkeypatch.setattr(base_mod, "verify_base_tx", verify)
    base_mod._used_base_tx_hashes.pop(VALID_TX_HASH, None)
    yield calls
    base_mod._used_base_tx_hashes.pop(VALID_TX_HASH, None)


async def _settle_base():
    return await base_mod.settle_base_payment(
        _mode_b_signature_header(VALID_TX_HASH, VALID_PAYER), _payment_requirements())


class TestBase:
    async def test_tx_hash_refused_across_restart(self, sb_semantics, base_ok):
        assert (await _settle_base())["success"] is True
        base_mod._used_base_tx_hashes.clear()
        again = await _settle_base()
        assert again["success"] is False
        assert again["reason"] == "replay_attack"
        assert base_ok["verify"] == 1  # refused before the RPC

    async def test_outage_fails_closed_then_retry_succeeds(self, sb_semantics, base_ok):
        sb_semantics.outage = True
        blocked = await _settle_base()
        assert blocked["success"] is False
        assert blocked["reason"].startswith("replay_check_unavailable")
        assert VALID_TX_HASH not in base_mod._used_base_tx_hashes

        sb_semantics.outage = False
        assert (await _settle_base())["success"] is True


# ── Stacks: settle_stacks_payment + the route's payment_id consume ──────────


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


async def _settle_stacks(tx):
    from agentpay._stacks_tx import txid_of
    return await stacks_pay.settle_stacks_payment(
        tx, txid_of(tx), payment_id=PAYMENT_ID,
        payment_payload={"network": "stacks:2147483648"},
        requirements={"scheme": "exact", "network": "stacks:2147483648",
                      "amount": "1", "asset": "sbtc", "payTo": GATEWAY_ADDR})


class TestStacksSettle:
    async def test_txid_refused_across_restart(self, sb_semantics, stacks_env):
        tx = _signed_tx(amount_sats=1)
        with respx.mock:
            broadcast = _hiro_broadcast_ok()
            _hiro_status("success")
            assert (await _settle_stacks(tx))["ok"] is True
            stacks_pay._used_stacks_txids.clear()
            again = await _settle_stacks(tx)
        assert again["state"] == "rejected"
        assert again["reason"] == "replay_attack"
        assert broadcast.call_count == 1

    async def test_outage_broadcasts_nothing(self, sb_semantics, stacks_env):
        tx = _signed_tx(amount_sats=1)
        sb_semantics.outage = True
        with respx.mock:
            broadcast = _hiro_broadcast_ok()
            res = await _settle_stacks(tx)
        assert res["state"] == "rejected"
        assert res["reason"].startswith("replay_store_unavailable")
        assert broadcast.call_count == 0
        assert not stacks_pay._used_stacks_txids


class TestStacksRoute:
    """The stale-nonce class: a re-signed tx for an already-consumed
    payment_id must be refused, and nothing broadcast for it."""

    class _Tool:
        name = "verified_route"
        price_usdc = "0.001"
        developer_address = ""

    @pytest.fixture(autouse=True)
    def _route(self, monkeypatch, sb_semantics, stacks_env):
        import gateway.routes.tools as rt
        self.rt = rt
        challenge = {"payment_id": PAYMENT_ID, "tool_name": "verified_route",
                     "amount_usdc": "0.001", "expires_at": 9999999999.0,
                     "stacks_sats": 1, "stacks_rate": "100000"}

        async def _lookup(pid):
            return challenge if pid == PAYMENT_ID else None

        monkeypatch.setattr(rt, "_lookup_challenge", _lookup)

    async def _settle(self, nonce):
        tx = _signed_tx(amount_sats=1, nonce=nonce)
        header = _header_for(tx)
        return await self.rt._settle_stacks_path(
            self._Tool(), "verified_route", header, json.loads(base64.b64decode(header)))

    async def test_second_tx_for_same_payment_id_refused(self, sb_semantics):
        with respx.mock:
            broadcast = _hiro_broadcast_ok()
            _hiro_status("success")
            first = await self._settle(nonce=4)
            assert isinstance(first, dict) and first["authorized"] is True
            retry = await self._settle(nonce=5)
        assert not isinstance(retry, dict)
        assert json.loads(retry.body)["error_reason"] == "payment_id_already_used_replay"
        assert broadcast.call_count == 1

    async def test_outage_before_broadcast_is_retryable(self, sb_semantics):
        sb_semantics.outage = True
        with respx.mock:
            broadcast = _hiro_broadcast_ok()
            blocked = await self._settle(nonce=4)
        assert json.loads(blocked.body)["error_reason"].startswith("replay_store_unavailable")
        assert broadcast.call_count == 0
        assert PAYMENT_ID not in sb_semantics.payment_ids

        sb_semantics.outage = False
        with respx.mock:
            _hiro_broadcast_ok()
            _hiro_status("success")
            ok = await self._settle(nonce=4)
        assert isinstance(ok, dict) and ok["authorized"] is True
