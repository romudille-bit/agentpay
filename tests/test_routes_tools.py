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

    def test_returns_all_14_tools(self, client):
        r = client.get("/tools")
        assert r.status_code == 200
        body = r.json()
        assert "tools" in body
        assert "count" in body
        assert body["count"] == 14
        assert len(body["tools"]) == 14

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
        assert body["price_usdc"] == "0.001"

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
        assert r.headers.get("x-price-usdc") == "0.001"
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
        # token_market_data is $0.001 — confirm the price is correct for
        # the *resolved* tool, not 0 or some other value
        assert body["amount_usdc"] == "0.001"

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
        assert body["payment"]["amount_usdc"] == "0.001"
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
