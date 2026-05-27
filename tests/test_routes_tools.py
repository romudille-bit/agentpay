"""
test_routes_tools.py — Integration tests for gateway/routes/tools.py.

Exercises the full HTTP surface via TestClient:
  GET  /tools                  — list
  GET  /tools/{name}           — single tool, alias resolution
  HEAD /tools/{name}/call      — pre-flight pricing headers
  POST /tools/{name}/call      — full x402 flow (no payment → 402;
                                  with payment → 200)
  POST /tools/register         — register a new tool

Mocks `verify_and_fulfill` and `real_tool_response` at the routes module
level so we exercise route logic without hitting Stellar Horizon or any
upstream tool API. The Stellar verification path itself is covered by
test_stellar.py; this file pins the *route* contract.

Conventions:
  - The `client` fixture (from conftest.py) sets KEEPALIVE_DISABLED=1
    and blanks SUPABASE_URL/KEY so startup is hermetic.
  - mock_settings (autoused via the client fixture) ensures
    GATEWAY_PUBLIC_KEY is a known test value, so 402 responses are
    deterministic.
"""

import pytest


# ── Fixtures specific to route tests ─────────────────────────────────────────

@pytest.fixture
def patch_route_verify(monkeypatch):
    """Replace verify_and_fulfill in routes.tools with a controllable mock.

    Returns a function the test can call to set the next response.
    Default: authorize any payment with the X-Payment's tx_hash echoed back.
    """
    state = {"behavior": "authorize"}

    async def fake_verify_and_fulfill(payment_header, agent_address):
        from gateway.x402 import parse_payment_header
        parsed = parse_payment_header(payment_header) or {}
        if state["behavior"] == "authorize":
            return {
                "authorized": True,
                "challenge": {"tool_name": "mocked", "amount_usdc": "0.001"},
                "tx_hash": parsed.get("tx_hash", ""),
                "network": "stellar-testnet",
            }
        if state["behavior"] == "replay":
            return {"authorized": False, "reason": "Payment already used (replay attack)"}
        if state["behavior"] == "expired":
            return {"authorized": False, "reason": "Payment ID not found or expired"}
        return {"authorized": False, "reason": "mocked verification failure"}

    import gateway.routes.tools
    monkeypatch.setattr(
        gateway.routes.tools, "verify_and_fulfill", fake_verify_and_fulfill
    )

    def set_behavior(b):
        state["behavior"] = b

    return set_behavior


@pytest.fixture
def patch_route_tool_response(monkeypatch):
    """Replace real_tool_response in routes.tools with a no-network mock."""
    async def fake_real_tool_response(tool_name, params):
        return {"tool": tool_name, "params": params, "mocked": True}

    import gateway.routes.tools
    monkeypatch.setattr(
        gateway.routes.tools, "real_tool_response", fake_real_tool_response
    )
    return fake_real_tool_response


# ── GET /tools — list endpoint ───────────────────────────────────────────────

class TestListTools:

    def test_returns_all_17_tools(self, client):
        r = client.get("/tools")
        assert r.status_code == 200
        body = r.json()
        assert "tools" in body
        assert "count" in body
        assert body["count"] == 17
        assert len(body["tools"]) == 17

    def test_each_tool_has_required_fields(self, client):
        r = client.get("/tools")
        for tool in r.json()["tools"]:
            assert "name" in tool
            assert "price_usdc" in tool
            assert "category" in tool

    def test_filter_by_category(self, client):
        r = client.get("/tools?category=defi")
        assert r.status_code == 200
        body = r.json()
        # Every returned tool should be category=defi
        for tool in body["tools"]:
            assert tool["category"] == "defi"


# ── GET /tools/{name} + HEAD pre-flight ──────────────────────────────────────

class TestGetTool:

    def test_known_tool_returns_details(self, client):
        r = client.get("/tools/token_price")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "token_price"
        assert body["price_usdc"] == "0.000"

    def test_unknown_tool_returns_404(self, client):
        r = client.get("/tools/nonexistent_tool")
        assert r.status_code == 404

    def test_legacy_alias_resolves(self, client):
        # dex_liquidity is a legacy alias for token_market_data
        r = client.get("/tools/dex_liquidity")
        assert r.status_code == 200
        # The resolved tool's canonical name should be returned
        assert r.json()["name"] == "token_market_data"

    def test_head_preflight_returns_pricing_headers(self, client):
        r = client.head("/tools/token_price/call")
        assert r.status_code == 200
        assert r.headers.get("x-price-usdc") == "0.000"
        assert r.headers.get("x-asset") == "USDC"
        assert "x-network" in r.headers
        assert r.headers.get("x-tool-name") == "token_price"

    def test_head_preflight_unknown_tool_404(self, client):
        r = client.head("/tools/nonexistent_tool/call")
        assert r.status_code == 404


# ── POST /tools/{name}/call — 402 challenge issuance ─────────────────────────

class TestCall402Challenge:
    """When no payment header is present, the gateway must issue a 402
    with all the fields an agent SDK needs to pay."""

    def test_no_payment_returns_402(self, client):
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
        )
        assert r.status_code == 402

    def test_402_body_has_required_fields(self, client):
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
        )
        body = r.json()
        assert "payment_id" in body
        assert "amount_usdc" in body
        assert "pay_to" in body
        assert body["asset"] == "USDC"
        assert "instructions" in body
        # x402 v2 structured options
        assert "payment_options" in body
        assert "stellar" in body["payment_options"]

    def test_402_includes_faucet_hint_in_instructions(self, client):
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
        )
        # The instructions field should mention the faucet for newcomers
        assert "/faucet" in r.json()["instructions"]

    def test_402_uses_resolved_alias_for_pricing(self, client):
        # POST /tools/dex_liquidity/call → 402 should price token_market_data
        r = client.post(
            "/tools/dex_liquidity/call",
            json={"parameters": {"token_a": "ETH", "token_b": "USDC"}},
        )
        assert r.status_code == 402
        body = r.json()
        # token_market_data is free ($0.000) — confirm the price is correct for
        # the *resolved* tool, not some other value
        assert body["amount_usdc"] == "0.000"

    def test_unknown_tool_post_returns_404(self, client):
        r = client.post(
            "/tools/nonexistent_tool/call",
            json={"parameters": {}},
        )
        assert r.status_code == 404


# ── POST /tools/{name}/call — full payment flow ──────────────────────────────

class TestCallWithPayment:
    """The payment-side branch. Uses patched verify_and_fulfill +
    real_tool_response so no Stellar / upstream API traffic happens."""

    def test_valid_payment_returns_200_with_tool_data(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        # Step 1: get a 402 challenge to extract the payment_id
        first = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
        )
        payment_id = first.json()["payment_id"]

        # Step 2: retry with a fake X-Payment header — verify_and_fulfill is
        # patched to accept anything
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={
                "X-Payment": f"tx_hash=mocktxhash,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENT",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tool"] == "token_price"
        assert body["result"]["mocked"] is True
        assert body["payment"]["amount_usdc"] == "0.000"
        assert body["payment"]["network"] == "stellar-testnet"

    def test_missing_agent_address_returns_400(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        # X-Payment present but no X-Agent-Address and no agent_address in body
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {}},
            headers={
                "X-Payment": "tx_hash=abc,from=GAGENT,id=test-id",
            },
        )
        assert r.status_code == 400
        assert "agent_address" in r.json()["detail"].lower()

    def test_failed_verification_returns_402(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        patch_route_verify("expired")
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {}},
            headers={
                "X-Payment": "tx_hash=abc,from=GAGENT,id=test-id",
                "X-Agent-Address": "GAGENT",
            },
        )
        assert r.status_code == 402
        assert "expired" in r.json()["reason"].lower()

    def test_replay_attack_returns_402(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        patch_route_verify("replay")
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {}},
            headers={
                "X-Payment": "tx_hash=abc,from=GAGENT,id=test-id",
                "X-Agent-Address": "GAGENT",
            },
        )
        assert r.status_code == 402
        assert "replay" in r.json()["reason"].lower()

    def test_alias_resolves_in_post_path(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        # POST /tools/dex_liquidity/call should also resolve to token_market_data
        # for both the 402 challenge AND the paid call. This regressed in Tier 1.
        first = client.post("/tools/dex_liquidity/call", json={"parameters": {}})
        payment_id = first.json()["payment_id"]
        r = client.post(
            "/tools/dex_liquidity/call",
            json={"parameters": {}},
            headers={
                "X-Payment": f"tx_hash=mocktx,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENT",
            },
        )
        assert r.status_code == 200
        # The dispatcher should be called with the *resolved* tool name
        assert r.json()["result"]["tool"] == "token_market_data"


# ── POST /tools/register ─────────────────────────────────────────────────────

class TestRegisterTool:

    def test_register_new_tool(self, client):
        r = client.post(
            "/tools/register",
            json={
                "name": "test_tool_xyz",
                "description": "A test tool registered by the suite",
                "endpoint": "https://example.com/tool",
                "price_usdc": "0.001",
                "developer_address": "GTESTDEV",
                "parameters": {"type": "object", "properties": {}},
                "category": "data",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "registered"
        assert body["tool"]["name"] == "test_tool_xyz"

    def test_register_duplicate_returns_409(self, client):
        # token_price already exists in the seed registry
        r = client.post(
            "/tools/register",
            json={
                "name": "token_price",  # collision
                "description": "duplicate",
                "endpoint": "https://example.com",
                "price_usdc": "0.001",
                "developer_address": "GTESTDEV",
                "parameters": {},
                "category": "data",
            },
        )
        assert r.status_code == 409


# ── PR #14: payment_logs lifecycle state machine ─────────────────────────────
#
# These pin the new pre-402 INSERT and the state PATCH transitions.
# mock_settings (the autouse fixture from conftest) forces sb_enabled
# to return False by default, so for these tests we override it back
# to True and mock out the underlying INSERT/PATCH calls.

@pytest.fixture
def supabase_lifecycle_capture(monkeypatch):
    """Enable Supabase at the route level and capture every state mutation.

    Returns a dict like:
      {"insert": [list of insert payloads], "update": [list of (id, state, fields)]}
    so tests can assert on the exact sequence of writes.

    sb_enabled needs to be patched in every module that does
    `from gateway.services.supabase import sb_enabled` — main, routes.tools,
    and services.supabase itself. The conftest does this for False; we
    flip them all back to True here.
    """
    captured = {"insert": [], "update": []}

    async def fake_insert(payment_id, tool_name, network, amount_usdc, **kw):
        captured["insert"].append({
            "payment_id": payment_id, "tool_name": tool_name,
            "network": network, "amount_usdc": amount_usdc, **kw,
        })
        return 999  # fake row id — non-None means success

    async def fake_update(payment_id, state, **fields):
        captured["update"].append({"payment_id": payment_id, "state": state, **fields})

    import gateway.routes.tools as routes_tools_mod
    import gateway.services.supabase as sb_mod

    enabled = lambda: True
    monkeypatch.setattr(sb_mod, "sb_enabled", enabled)
    if hasattr(routes_tools_mod, "sb_enabled"):
        monkeypatch.setattr(routes_tools_mod, "sb_enabled", enabled)
    monkeypatch.setattr(routes_tools_mod, "insert_pending_payment_log", fake_insert)
    monkeypatch.setattr(routes_tools_mod, "update_payment_log_state", fake_update)

    return captured


class TestLifecycleStateMachine:

    def test_402_response_creates_pending_row(self, client, supabase_lifecycle_capture):
        """Pre-402 INSERT fires with state='pending' before the 402 returns.
        Closes the analytics gap from §5.1 of the design doc — every
        challenge is captured, not just paid ones."""
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
        )
        assert r.status_code == 402

        # Exactly one INSERT, keyed on the 402 challenge's payment_id
        assert len(supabase_lifecycle_capture["insert"]) == 1
        row = supabase_lifecycle_capture["insert"][0]
        assert row["tool_name"] == "token_price"
        assert row["network"] == "stellar-testnet"
        assert row["amount_usdc"] == "0.000"
        # payment_id matches what we returned in the body
        assert row["payment_id"] == r.json()["payment_id"]

    def test_supabase_insert_failure_returns_503(self, client, monkeypatch):
        """Fail-closed: when sb_enabled is True but the INSERT returns None
        (Supabase write failed), refuse to issue the challenge with 503.
        The gateway never advertises a payment it can't track."""
        import gateway.routes.tools as routes_tools_mod
        import gateway.services.supabase as sb_mod

        enabled = lambda: True
        monkeypatch.setattr(sb_mod, "sb_enabled", enabled)
        if hasattr(routes_tools_mod, "sb_enabled"):
            monkeypatch.setattr(routes_tools_mod, "sb_enabled", enabled)

        async def fake_insert_fails(*args, **kw):
            return None  # simulates Supabase 5xx / network error
        monkeypatch.setattr(
            routes_tools_mod, "insert_pending_payment_log", fake_insert_fails
        )

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
        )
        assert r.status_code == 503
        assert "challenge issuance refused" in r.json()["detail"].lower()

    def test_replay_attempt_marks_rejected(
        self, client, supabase_lifecycle_capture, patch_route_verify
    ):
        """A replay attempt should PATCH the pending row to 'rejected'
        with error_reason populated. Lets analytics distinguish replay
        attacks from abandoned challenges."""
        patch_route_verify("replay")

        # Issue the 402 first to plant the pending row
        first = client.post("/tools/token_price/call", json={"parameters": {}})
        payment_id = first.json()["payment_id"]

        # Now retry with a (mocked) replay
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {}},
            headers={
                "X-Payment": f"tx_hash=replayhash,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENT",
            },
        )
        assert r.status_code == 402
        assert "replay" in r.json()["reason"].lower()

        # Lifecycle: one INSERT (pending) at challenge issue + one PATCH (rejected)
        rejected = [u for u in supabase_lifecycle_capture["update"] if u["state"] == "rejected"]
        assert len(rejected) == 1
        assert rejected[0]["payment_id"] == payment_id
        assert "replay" in rejected[0].get("error_reason", "").lower()

    def test_happy_path_transitions_pending_to_payment_done(
        self, client, supabase_lifecycle_capture,
        patch_route_verify, patch_route_tool_response,
    ):
        """The full success trail: pending (insert) → verified (PATCH) →
        payment_done (PATCH). split_done is fired from inside split_payment
        which is mocked at the routes layer; covered by test_x402 for the
        x402.py side. verified is fire-and-forget per the Q3 decision."""
        first = client.post("/tools/token_price/call", json={"parameters": {"symbol": "ETH"}})
        payment_id = first.json()["payment_id"]

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={
                "X-Payment": f"tx_hash=happyhash,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENT",
            },
        )
        assert r.status_code == 200

        # Insert at challenge issue
        assert len(supabase_lifecycle_capture["insert"]) == 1
        assert supabase_lifecycle_capture["insert"][0]["payment_id"] == payment_id

        # State machine PATCHes
        states_for_pid = [
            u["state"] for u in supabase_lifecycle_capture["update"]
            if u["payment_id"] == payment_id
        ]
        assert "verified" in states_for_pid
        assert "payment_done" in states_for_pid
        # payment_done must come AFTER verified
        assert states_for_pid.index("payment_done") > states_for_pid.index("verified")

        # The payment_done PATCH carries the analytics columns
        payment_done = next(
            u for u in supabase_lifecycle_capture["update"]
            if u["payment_id"] == payment_id and u["state"] == "payment_done"
        )
        assert payment_done.get("network") == "stellar-testnet"
        assert payment_done.get("gateway_fee_usdc") is not None

    def test_tool_failure_post_verify_marks_refund_pending(
        self, client, supabase_lifecycle_capture, monkeypatch, patch_route_verify,
    ):
        """When the tool execution itself fails AFTER the payment is
        verified, transition the row to 'refund_pending'. Refund logic
        is deferred to #12; #14 just sets the marker state so #12 has
        a clean handoff. error_reason captures what went wrong."""
        # Make the tool dispatcher blow up post-verify
        async def boom(tool_name, params):
            raise RuntimeError("upstream API exploded")

        import gateway.routes.tools as routes_tools_mod
        monkeypatch.setattr(routes_tools_mod, "real_tool_response", boom)

        first = client.post("/tools/token_price/call", json={"parameters": {}})
        payment_id = first.json()["payment_id"]

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {}},
            headers={
                "X-Payment": f"tx_hash=goodhash,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENTAGENTAGENTAGENTAGENTAGENTAGENT",
            },
        )
        # The 502 is the user-facing signal that the tool failed
        assert r.status_code == 502

        # The row transitioned: pending → verified (fire) → refund_pending (awaited)
        states_for_pid = [
            u["state"] for u in supabase_lifecycle_capture["update"]
            if u["payment_id"] == payment_id
        ]
        assert "refund_pending" in states_for_pid

        refund_row = next(
            u for u in supabase_lifecycle_capture["update"]
            if u["payment_id"] == payment_id and u["state"] == "refund_pending"
        )
        assert "tool_exec_failed" in refund_row.get("error_reason", "")
        # payment_done must NOT have been written for a failed tool call
        assert "payment_done" not in states_for_pid

    def test_tool_failure_response_body_dark_launch(
        self, client, supabase_lifecycle_capture, monkeypatch, patch_route_verify,
    ):
        """PR #12: with REFUND_ENABLED=False (the default), the response
        body carries payment_status='refund_disabled' so the SDK can
        distinguish 'we'd refund if the flag were on' from 'we will
        refund'. refund_eta_seconds is null.
        """
        # The conftest mock_settings sets REFUND_ENABLED to whatever the
        # cached settings has. We don't override here — default is False.
        async def boom(tool_name, params):
            raise RuntimeError("ETIMEDOUT")
        import gateway.routes.tools as routes_tools_mod
        monkeypatch.setattr(routes_tools_mod, "real_tool_response", boom)

        first = client.post("/tools/token_price/call", json={"parameters": {}})
        payment_id = first.json()["payment_id"]

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {}},
            headers={
                "X-Payment": f"tx_hash=darkhash,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENTAGENTAGENTAGENTAGENTAGENTAGENT",
            },
        )
        assert r.status_code == 502
        body = r.json()
        assert body["error"] == "Tool execution failed"
        assert body["payment_id"] == payment_id
        assert body["payment_status"] == "refund_disabled"
        assert body["refund_eta_seconds"] is None
        assert "tool_exec_failed" in body["error_reason"]
        assert "ETIMEDOUT" in body["error_reason"]

    def test_tool_failure_response_body_flag_on(
        self, client, supabase_lifecycle_capture, monkeypatch, patch_route_verify,
    ):
        """PR #12: with REFUND_ENABLED=True, the response body switches
        to payment_status='refund_pending' + refund_eta_seconds=60.
        The actual on-chain refund is handled by the background worker;
        the response is forward-looking advice for the SDK.
        """
        import gateway.routes.tools as routes_tools_mod
        # Patch settings.REFUND_ENABLED to True for this test
        from gateway.config import get_settings
        get_settings.cache_clear()
        new_settings = get_settings()
        new_settings.REFUND_ENABLED = True
        monkeypatch.setattr(routes_tools_mod, "settings", new_settings)

        async def boom(tool_name, params):
            raise RuntimeError("upstream 503")
        monkeypatch.setattr(routes_tools_mod, "real_tool_response", boom)

        first = client.post("/tools/token_price/call", json={"parameters": {}})
        payment_id = first.json()["payment_id"]

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {}},
            headers={
                "X-Payment": f"tx_hash=hothash,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENTAGENTAGENTAGENTAGENTAGENTAGENT",
            },
        )
        assert r.status_code == 502
        body = r.json()
        assert body["payment_status"] == "refund_pending"
        assert body["refund_eta_seconds"] == 60
