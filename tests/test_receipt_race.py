"""
test_receipt_race.py — AGE-58 regression: the Base-path receipt-write race.

The tx-hash-keyed 'verified' row used to be inserted with a fire-and-forget
asyncio.create_task while the terminal `await update_payment_log_state(...,
"payment_done")` ran right after. If the PATCH won the race it no-op'd on the
missing row, then the insert landed 'verified' — and the row never advanced
(the "stuck in verified / phantom-abandon" class, AGE-52). It hit both
/tools/{name}/call (Base) and /v1/session/create (the KPI).

These tests make the insert SLOW (several event-loop ticks) and assert the
insert still completes before any terminal state write starts. Pre-fix, the
PATCH is recorded first and the tests fail.
"""

import asyncio

import pytest


@pytest.fixture
def race_capture(monkeypatch):
    """Ordered event log with a deliberately slow insert, patched into BOTH
    route modules. Events: ("insert_started"|"insert_landed", payment_id) and
    ("update", payment_id, state)."""
    events: list[tuple] = []

    async def slow_insert(payment_id=None, **kw):
        events.append(("insert_started", payment_id))
        # Slow enough that any code path NOT awaiting the task reaches its
        # terminal PATCH first — including tool execution in between. Keeps
        # the tests deterministic rather than winning by scheduler luck.
        await asyncio.sleep(0.75)
        events.append(("insert_landed", payment_id))
        return 999

    async def fake_update(payment_id, state, **fields):
        events.append(("update", payment_id, state))

    async def fake_correlate(**kw):
        return None

    import gateway.routes.session as rs
    import gateway.routes.tools as rt
    import gateway.services.supabase as sb_mod

    enabled = lambda: True
    monkeypatch.setattr(sb_mod, "sb_enabled", enabled)
    for mod in (rt, rs):
        monkeypatch.setattr(mod, "sb_enabled", enabled)
        monkeypatch.setattr(mod, "insert_pending_payment_log", slow_insert)
        monkeypatch.setattr(mod, "update_payment_log_state", fake_update)
        monkeypatch.setattr(mod, "correlate_pending_challenge", fake_correlate)
    return events


@pytest.fixture
def base_settle_ok(monkeypatch):
    """Mock a successful Base settlement in both route modules."""
    async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
        return {
            "success": True,
            "tx_hash": "0x" + "a" * 64,
            "payer":   "0x" + "b" * 40,
            "network": "eip155:8453",
            "reason":  "ok",
        }

    import gateway.routes.session as rs
    import gateway.routes.tools as rt
    monkeypatch.setattr(rt.base_pay, "settle_base_payment", fake_settle)
    monkeypatch.setattr(rs.base_pay, "settle_base_payment", fake_settle)
    monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
    monkeypatch.setattr(rs.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)


def _v2_sig():
    import base64
    import json
    return base64.b64encode(json.dumps(
        {"x402Version": 2, "payload": {"signature": "0xfake"}}
    ).encode()).decode()


def _assert_insert_lands_before_terminal_write(events, tx_key: str):
    """The core AGE-58 invariant: for the tx-keyed row, the insert must have
    LANDED before any terminal state write on that key starts."""
    tx_events = [e for e in events if e[1] == tx_key]
    landed = [i for i, e in enumerate(tx_events) if e[0] == "insert_landed"]
    updates = [i for i, e in enumerate(tx_events) if e[0] == "update"]
    assert landed, f"tx-keyed insert never landed; events: {events}"
    assert updates, f"no terminal write on the tx key; events: {events}"
    assert landed[0] < updates[0], (
        f"terminal write raced ahead of the row insert (AGE-58): {tx_events}"
    )


class TestToolsRouteReceiptRace:

    def test_base_payment_done_waits_for_row_insert(
        self, client, race_capture, base_settle_ok, monkeypatch,
    ):
        """Happy path: tx-keyed 'verified' insert must land before the
        payment_done PATCH — even when the insert is slow."""
        import gateway.routes.tools as rt

        async def fake_tool(tool_name, params):
            return {"verdict": "ok"}
        monkeypatch.setattr(rt, "real_tool_response", fake_tool)

        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000, "side": "long"}},
            headers={"PAYMENT-SIGNATURE": _v2_sig()},
        )
        assert r.status_code == 200
        tx_key = "0x" + "a" * 64
        _assert_insert_lands_before_terminal_write(race_capture, tx_key)
        # And the terminal state actually advanced past 'verified'.
        terminal = [e for e in race_capture if e[0] == "update" and e[1] == tx_key]
        assert terminal[-1][2] == "payment_done"

    def test_base_refund_path_waits_for_row_insert(
        self, client, race_capture, base_settle_ok, monkeypatch,
    ):
        """Tool blows up post-settle: the refund-side state write must also
        wait for the row insert, or the refund marker lands on nothing."""
        import gateway.routes.tools as rt

        async def boom(tool_name, params):
            raise RuntimeError("upstream exploded")
        monkeypatch.setattr(rt, "real_tool_response", boom)

        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000, "side": "long"}},
            headers={"PAYMENT-SIGNATURE": _v2_sig()},
        )
        assert r.status_code == 502
        _assert_insert_lands_before_terminal_write(race_capture, "0x" + "a" * 64)


class TestSessionCreateReceiptRace:

    def test_session_create_payment_done_waits_for_row_insert(
        self, client, race_capture, base_settle_ok,
    ):
        """session_create IS the KPI — its Base rows were the ones stranding
        in 'verified'."""
        r = client.post(
            "/v1/session/create",
            json={"agent_address": "0x" + "d" * 40, "max_spend": "0.10"},
            headers={"PAYMENT-SIGNATURE": _v2_sig()},
        )
        assert r.status_code == 200
        tx_key = "0x" + "a" * 64
        _assert_insert_lands_before_terminal_write(race_capture, tx_key)
        terminal = [e for e in race_capture if e[0] == "update" and e[1] == tx_key]
        assert terminal[-1][2] == "payment_done"
