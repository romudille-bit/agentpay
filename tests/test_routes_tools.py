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

    def test_returns_all_20_tools(self, client):
        r = client.get("/tools")
        assert r.status_code == 200
        body = r.json()
        assert "tools" in body
        assert "count" in body
        assert body["count"] == 20
        assert len(body["tools"]) == 20

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
        # AGE-134: HEAD answers 402 (mirroring the GET probe) so external
        # probers never score a healthy resource as "not returning 402".
        # The pre-flight pricing headers are preserved; only the status
        # changed (200 → 402).
        r = client.head("/tools/token_price/call")
        assert r.status_code == 402
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

    # ── AGE-42: paid tool errors must refund, not charge ──────────────────

    def _pay(self, client, tool, error_result=None):
        """Drive the paid Stellar flow for `tool`; executor returns error_result
        when given, else a normal mocked payload."""
        import gateway.routes.tools as rt
        first = client.post(f"/tools/{tool}/call", json={"parameters": {}})
        payment_id = first.json()["payment_id"]
        return client.post(
            f"/tools/{tool}/call",
            json={"parameters": {}},
            headers={
                "X-Payment": f"tx_hash=mocktx,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENT",
            },
        )

    def test_paid_tool_error_result_returns_502_refund(
        self, client, patch_route_verify, monkeypatch
    ):
        """A PAID tool whose executor returns {'error': ...} must NOT respond
        200 'payment_done' — it charged the agent for nothing (live incident:
        session_create via /tools/…/call, 2026-07-13). Contract: 502 with
        payment_status so SDK callers get RefundPending."""
        import gateway.routes.tools as rt
        async def erroring(tool_name, params):
            return {"error": "upstream exploded"}
        monkeypatch.setattr(rt, "real_tool_response", erroring)

        r = self._pay(client, "pre_trade_check")   # $0.01 — a paid tool
        assert r.status_code == 502
        body = r.json()
        assert body["payment_status"] in ("refund_pending", "refund_disabled")
        assert "upstream exploded" in body["error_reason"]

    def test_free_tool_error_result_still_returns_200(
        self, client, patch_route_verify, monkeypatch
    ):
        """$0 tools keep the legacy in-band error (nothing was charged, and the
        free x402 lifecycle the analytics pin must not gain a 502 branch)."""
        import gateway.routes.tools as rt
        async def erroring(tool_name, params):
            return {"error": "upstream exploded"}
        monkeypatch.setattr(rt, "real_tool_response", erroring)

        r = self._pay(client, "token_price")       # free tool
        assert r.status_code == 200
        assert r.json()["result"]["error"] == "upstream exploded"

    def test_session_create_via_tools_route_returns_session(
        self, client, patch_route_verify
    ):
        """AGE-42 part 2: session_create now has a real executor on the generic
        tools route — paying it returns a session, not 'no implementation'."""
        r = self._pay(client, "session_create")
        assert r.status_code == 200
        result = r.json()["result"]
        assert "error" not in result
        assert result["session_id"]
        assert result["tools_endpoint"].endswith("/tools")
        assert "session" in result["sdk_hint"].lower()

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
    """AGE-59: /tools/register is secret-gated + validated. 404 with no
    secret configured, 401 on a bad secret, 422 on invalid fields."""

    SECRET = "test-register-secret-0123456789abcdef"
    # Valid Stellar strkey (the mainnet gateway wallet — checksum-valid).
    DEV = "GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2"

    def _payload(self, **overrides):
        base = {
            "name": "test_tool_xyz",
            "description": "A test tool registered by the suite",
            # Literal public IP, not a hostname: getaddrinfo parses it locally
            # with no DNS lookup, so the SSRF guard's public-IP check runs
            # deterministically even on a box with no outbound DNS (the guard
            # correctly fails closed when a hostname can't resolve).
            "endpoint": "https://8.8.8.8/tool",
            "price_usdc": "0.001",
            "developer_address": self.DEV,
            "parameters": {"type": "object", "properties": {}},
            "category": "data",
        }
        base.update(overrides)
        return base

    def _enable(self, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "TOOL_REGISTER_SECRET", self.SECRET)

    def test_register_disabled_without_secret_config(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "TOOL_REGISTER_SECRET", "")
        r = client.post("/tools/register", json=self._payload())
        assert r.status_code == 404

    def test_register_wrong_secret_is_401(self, client, monkeypatch):
        self._enable(monkeypatch)
        r = client.post(
            "/tools/register", json=self._payload(),
            headers={"X-Register-Secret": "wrong"},
        )
        assert r.status_code == 401
        # Missing header is 401 too, not a 500.
        assert client.post("/tools/register", json=self._payload()).status_code == 401

    def test_register_new_tool_with_secret(self, client, monkeypatch):
        self._enable(monkeypatch)
        r = client.post(
            "/tools/register", json=self._payload(),
            headers={"X-Register-Secret": self.SECRET},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "registered"
        assert body["tool"]["name"] == "test_tool_xyz"

    def test_register_duplicate_returns_409(self, client, monkeypatch):
        self._enable(monkeypatch)
        r = client.post(
            "/tools/register",
            json=self._payload(name="token_price"),  # collision with seed
            headers={"X-Register-Secret": self.SECRET},
        )
        assert r.status_code == 409

    def test_register_persists_and_reports_true(self, client, monkeypatch):
        """AGE-71: a successful registration is pushed to Supabase and the
        response tells the caller it will survive a restart."""
        import gateway.routes.tools as rt

        async def _ok(_tool_dict):
            return True

        self._enable(monkeypatch)
        monkeypatch.setattr(rt, "persist_tool_registration", _ok)
        r = client.post(
            "/tools/register", json=self._payload(name="persist_ok_tool"),
            headers={"X-Register-Secret": self.SECRET},
        )
        assert r.status_code == 200
        assert r.json()["persisted"] is True

    def test_register_survives_persist_failure_but_flags_it(self, client, monkeypatch):
        """AGE-71: a Supabase blip must NOT fail the registration (the tool is
        live in-memory) — but the response must flag persisted=False so the
        caller knows it won't outlive a redeploy."""
        import gateway.routes.tools as rt

        async def _fail(_tool_dict):
            return False

        self._enable(monkeypatch)
        monkeypatch.setattr(rt, "persist_tool_registration", _fail)
        r = client.post(
            "/tools/register", json=self._payload(name="persist_fail_tool"),
            headers={"X-Register-Secret": self.SECRET},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "registered"
        assert body["persisted"] is False

    def test_register_rejects_bad_fields(self, client, monkeypatch):
        self._enable(monkeypatch)
        cases = [
            ({"developer_address": "GTESTDEV"}, "developer_address"),   # not a strkey
            ({"developer_address": "0x1234"}, "developer_address"),     # short EVM
            ({"price_usdc": "-1"}, "price_usdc"),
            ({"price_usdc": "50"}, "price_usdc"),                       # above cap
            ({"price_usdc": "NaN"}, "price_usdc"),
            ({"name": "Bad Name!"}, "name"),
            ({"category": "weird"}, "category"),
            ({"endpoint": "http://example.com/tool"}, "endpoint"),      # not https
        ]
        for overrides, field in cases:
            r = client.post(
                "/tools/register", json=self._payload(**overrides),
                headers={"X-Register-Secret": self.SECRET},
            )
            assert r.status_code == 422, (overrides, r.status_code, r.text)
            assert field in r.json()["detail"], (overrides, r.text)

    def test_register_rejects_private_endpoints(self, client, monkeypatch):
        """SSRF: endpoints resolving to loopback/private/link-local
        (incl. the cloud metadata address) must be rejected."""
        self._enable(monkeypatch)
        for bad in (
            "https://127.0.0.1/steal",
            "https://169.254.169.254/latest/meta-data/",
            "https://10.0.0.5/internal",
            "https://192.168.1.1/router",
            "https://localhost/loop",
        ):
            r = client.post(
                "/tools/register", json=self._payload(endpoint=bad),
                headers={"X-Register-Secret": self.SECRET},
            )
            assert r.status_code == 422, (bad, r.status_code, r.text)
            assert "endpoint" in r.json()["detail"], bad


class TestEndpointSafety:
    """AGE-59: _endpoint_is_safe — the shared registration/call-time guard."""

    def test_blocks_non_https_and_private(self):
        from gateway.routes.tools import _endpoint_is_safe
        assert _endpoint_is_safe("http://example.com/x")[0] is False
        assert _endpoint_is_safe("ftp://example.com/x")[0] is False
        assert _endpoint_is_safe("https://127.0.0.1/x")[0] is False
        assert _endpoint_is_safe("https://169.254.169.254/x")[0] is False
        assert _endpoint_is_safe("https://[::1]/x")[0] is False
        assert _endpoint_is_safe("not a url")[0] is False

    def test_allows_public_https(self):
        # Literal public IP → parsed locally, no DNS, deterministic offline.
        from gateway.routes.tools import _endpoint_is_safe
        ok, why = _endpoint_is_safe("https://8.8.8.8/tool")
        assert ok is True, why

    def test_allows_public_hostname_when_dns_available(self, monkeypatch):
        # Hostname path: mock getaddrinfo so the test never depends on the
        # box having outbound DNS. Pins that a hostname resolving to a public
        # IP is allowed.
        import gateway.routes.tools as rt
        monkeypatch.setattr(
            rt.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        ok, why = rt._endpoint_is_safe("https://example.com/tool")
        assert ok is True, why

    def test_hostname_resolving_private_is_blocked(self, monkeypatch):
        # DNS-rebinding shape: a hostname that resolves to a private IP is
        # rejected even though the name looks innocuous.
        import gateway.routes.tools as rt
        monkeypatch.setattr(
            rt.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("10.0.0.7", 443))],
        )
        ok, why = rt._endpoint_is_safe("https://totally-fine.example/tool")
        assert ok is False

    @pytest.mark.asyncio
    async def test_run_tool_blocks_unsafe_endpoint_at_call_time(self, monkeypatch):
        """A tool whose endpoint turned private (rebinding / pre-guard
        registration) must NOT be POSTed to — degrade to real APIs."""
        import gateway.routes.tools as rt

        called = {}

        async def fake_real(resolved, params):
            called["real"] = True
            return {"ok": 1}
        monkeypatch.setattr(rt, "real_tool_response", fake_real)

        class T:
            endpoint = "https://127.0.0.1/internal"
        out = await rt._run_tool(T(), "token_price", "token_price", {})
        assert out == {"ok": 1}
        assert called.get("real") is True


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
        """Pre-402 INSERT fires with state='pending' before the 402 returns —
        for PAID tools. (Disk-IO fix 2026-08-04: $0 tools no longer write a
        pending row; their 402 volume is counted in probe_rollup instead —
        see test_free_402_writes_no_pending_row.)"""
        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000}},
        )
        assert r.status_code == 402

        # Exactly one INSERT, keyed on the 402 challenge's payment_id
        assert len(supabase_lifecycle_capture["insert"]) == 1
        row = supabase_lifecycle_capture["insert"][0]
        assert row["tool_name"] == "pre_trade_check"
        assert row["network"] == "stellar-testnet"
        assert row["amount_usdc"] == "0.01"
        # payment_id matches what we returned in the body
        assert row["payment_id"] == r.json()["payment_id"]

    def test_free_402_writes_no_pending_row_but_is_counted(
        self, client, supabase_lifecycle_capture, monkeypatch,
    ):
        """Disk-IO fix (2026-08-04): a 402 on a $0 tool writes NOTHING
        per-event — no payment_logs pending row (99.5% of the table was
        abandoned bot probes) — but the issuance IS counted in the
        probe_rollup aggregate so the market signal survives."""
        from gateway.services import probe_rollup
        probe_rollup._counts.clear()

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
        )
        assert r.status_code == 402
        assert r.json()["payment_id"]          # challenge still fully issued
        assert supabase_lifecycle_capture["insert"] == []

        # Counted once, under kind='free_402', keyed by tool
        assert sum(
            n for (day, tool, ua, kind), n in probe_rollup._counts.items()
            if tool == "token_price" and kind == "free_402"
        ) == 1

    def test_get_probe_persists_nothing(self, client, supabase_lifecycle_capture):
        """GET discovery probes (crawlers) issue a valid 402 with zero DB
        writes: no payment_logs row (F6) and — disk-IO fix — no
        pending_challenges mirror either."""
        import gateway.x402 as x402_mod
        calls = {"n": 0}
        real = x402_mod.sb.store_pending_challenge

        async def counting_store(**kw):
            calls["n"] += 1
        x402_mod.sb.store_pending_challenge = counting_store
        try:
            r = client.get("/tools/pre_trade_check/call")
            assert r.status_code == 402
            assert supabase_lifecycle_capture["insert"] == []
            assert calls["n"] == 0
        finally:
            x402_mod.sb.store_pending_challenge = real

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
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000}},
        )
        assert r.status_code == 503
        assert "challenge issuance refused" in r.json()["detail"].lower()

    def test_free_402_survives_supabase_outage(self, client, monkeypatch):
        """Disk-IO fix companion to fail-closed: a $0 tool's 402 does not
        depend on Supabase at all, so an outage (which 503s paid
        challenges) must NOT break the free funnel or crawler probes."""
        import gateway.routes.tools as routes_tools_mod
        import gateway.services.supabase as sb_mod

        enabled = lambda: True
        monkeypatch.setattr(sb_mod, "sb_enabled", enabled)
        monkeypatch.setattr(routes_tools_mod, "sb_enabled", enabled)

        async def fake_insert_fails(*args, **kw):
            return None
        monkeypatch.setattr(
            routes_tools_mod, "insert_pending_payment_log", fake_insert_fails
        )

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
        )
        assert r.status_code == 402

    def test_replay_attempt_marks_rejected(
        self, client, supabase_lifecycle_capture, patch_route_verify
    ):
        """A replay attempt should PATCH the pending row to 'rejected'
        with error_reason populated. Lets analytics distinguish replay
        attacks from abandoned challenges."""
        patch_route_verify("replay")

        # Issue the 402 first to plant the pending row (paid tool — free
        # tools no longer have a pending row to mark)
        first = client.post("/tools/pre_trade_check/call", json={"parameters": {}})
        payment_id = first.json()["payment_id"]

        # Now retry with a (mocked) replay
        r = client.post(
            "/tools/pre_trade_check/call",
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
        x402.py side. verified is fire-and-forget per the Q3 decision.
        Paid tool — the free happy path is single-INSERT, tested below."""
        first = client.post("/tools/pre_trade_check/call", json={"parameters": {"symbol": "ETH"}})
        payment_id = first.json()["payment_id"]

        r = client.post(
            "/tools/pre_trade_check/call",
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

    def test_free_happy_path_writes_one_complete_row(
        self, client, supabase_lifecycle_capture,
        patch_route_verify, patch_route_tool_response,
    ):
        """Disk-IO fix (2026-08-04): a settled FREE call produces exactly ONE
        payment_logs write — a complete INSERT with state='payment_done' —
        instead of the old pending-INSERT + verified-PATCH + done-PATCH
        trail. Every settled call still lands in payment_logs (the
        analytics-lifecycle invariant), in a single round trip."""
        first = client.post("/tools/token_price/call", json={"parameters": {"symbol": "ETH"}})
        payment_id = first.json()["payment_id"]

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={
                "X-Payment": f"tx_hash=free:{payment_id},from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENT",
            },
        )
        assert r.status_code == 200

        # No pending insert at 402 time; ONE complete insert at settle time
        assert len(supabase_lifecycle_capture["insert"]) == 1
        row = supabase_lifecycle_capture["insert"][0]
        assert row["state"] == "payment_done"
        assert row["tool_name"] == "token_price"
        assert row["amount_usdc"] == "0.000"
        assert row["tx_hash"] == f"free:{payment_id}"
        assert row["agent_address"].startswith("GAGENT")
        assert row["parameters"] == {"symbol": "ETH"}

        # No terminal PATCH for the free path (the fire-and-forget
        # 'verified' PATCH may appear; 'payment_done' must not be a PATCH)
        done_patches = [
            u for u in supabase_lifecycle_capture["update"]
            if u["state"] == "payment_done"
        ]
        assert done_patches == []

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


# ── X-PAYMENT / PAYMENT-SIGNATURE header collision (Phase 1.1 follow-up) ─────
#
# x402-v2 clients (incl. SDK <= 0.2.3) send the same base64 v2 payload in BOTH
# X-PAYMENT and PAYMENT-SIGNATURE. X-Payment used to win routing, fail the
# legacy Stellar parse, and reject the call before the valid Base signature
# was considered — no Mode A named-tool call could ever succeed.

class TestHeaderCollision:

    def _both_headers(self):
        import base64, json
        v2_payload = base64.b64encode(json.dumps(
            {"x402Version": 2, "payload": {"signature": "0xfake"}}
        ).encode()).decode()
        return v2_payload

    def test_v2_payload_in_both_headers_routes_to_base(
        self, client, monkeypatch, patch_route_tool_response
    ):
        import gateway.routes.tools as rt

        async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
            return {
                "success": True,
                "tx_hash": "0x" + "a" * 64,
                "payer":   "0x" + "b" * 40,
                "network": "eip155:8453",
                "reason":  "ok",
            }
        monkeypatch.setattr(rt.base_pay, "settle_base_payment", fake_settle)
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        # PAID tool: free ($0) tools now route v2 payloads to _settle_free_v2
        # (wall E fix), so the base-settle routing must be pinned on a priced
        # tool.
        v2 = self._both_headers()
        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000, "side": "long"}},
            headers={"X-PAYMENT": v2, "PAYMENT-SIGNATURE": v2},
        )
        assert r.status_code == 200
        assert r.json()["payment"]["network"] == "eip155:8453"

    def test_valid_stellar_header_still_wins(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        # A real legacy Stellar proof must keep taking the Stellar path even
        # if a PAYMENT-SIGNATURE is also present.
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}, "agent_address": "GAGENT"},
            headers={
                "X-Payment": "tx_hash=abc,from=GAGENT,id=uuid-1",
                "PAYMENT-SIGNATURE": self._both_headers(),
            },
        )
        assert r.status_code == 200
        assert r.json()["payment"]["tx_hash"] == "abc"


# ── Per-wallet rate limiting (Phase 1.3) ─────────────────────────────────────

class TestWalletRateLimit:
    """The /tools/{name}/call limit is keyed on the declared X-Agent-Address
    (IP fallback), so one wallet can't dodge limits by rotating IPs and one
    busy wallet doesn't starve others behind the same IP."""

    def test_same_wallet_hits_limit(self, client):
        wallet = "GRATELIMITWALLETAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        statuses = []
        for _ in range(61):
            r = client.post(
                "/tools/token_price/call",
                json={"parameters": {"symbol": "ETH"}},
                headers={"X-Agent-Address": wallet},
            )
            statuses.append(r.status_code)
        assert statuses[:60] == [402] * 60   # challenges issued normally
        assert statuses[60] == 429           # 61st call from same wallet limited

    def test_other_wallet_unaffected(self, client):
        # Fill wallet A's bucket, then wallet B must still get a 402.
        for _ in range(60):
            client.post(
                "/tools/token_price/call",
                json={"parameters": {"symbol": "ETH"}},
                headers={"X-Agent-Address": "GWALLETAAAAAAAAAAAAAAAAAAAAAAAA"},
            )
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={"X-Agent-Address": "GWALLETBBBBBBBBBBBBBBBBBBBBBBBB"},
        )
        assert r.status_code == 402


# ── Bazaar metadata on the live 402 (Phase A3) ───────────────────────────────

class TestPreTradeCheckBazaar:
    """Bazaar's validation crawl reads extensions.bazaar + serviceName/tags
    from the LIVE 402 — without them the listing never leaves 'processing'."""

    def test_session_create_tool_path_declares_canonical_resource(
            self, client, monkeypatch):
        """AGE-112: session_create is payable at /tools/session_create/call AND
        /v1/session/create. The tool path used to publish no serviceName and its
        own resource url, so paying it settled a second, unnamed resource and
        never refreshed the real listing. Both paths must declare the same one."""
        import base64, json
        import gateway.routes.tools as rt
        from gateway.routes.session import SESSION_RESOURCE_URL
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post("/tools/session_create/call",
                        json={"parameters": {"max_spend": "0.10"}})
        assert r.status_code == 402
        h = r.headers["PAYMENT-REQUIRED"]
        payload = json.loads(base64.b64decode(h + "=" * (-len(h) % 4)))
        res = payload["resource"]
        assert res["serviceName"] == "AgentPay Spend Cap & Receipts"
        assert res["url"] == SESSION_RESOURCE_URL
        assert "session" in res["tags"]
        assert "bazaar" in payload.get("extensions", {})

    def test_402_carries_bazaar_extension(self, client, monkeypatch):
        import base64, json
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post("/tools/pre_trade_check/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        header = r.headers.get("PAYMENT-REQUIRED")
        assert header, "PAYMENT-REQUIRED header missing"
        payload = json.loads(base64.b64decode(header + "=" * (-len(header) % 4)))
        # AGE-36 readout: head terms are won by keyword-in-name, so the
        # Bazaar serviceName must carry what the tool DOES, not just the brand
        # (which 8 rival "AgentPay" products share). Pinned per-tool.
        assert payload["resource"]["serviceName"] == "AgentPay Pre-Trade Risk Check"
        assert "pre-trade-check" in payload["resource"]["tags"]
        assert "bazaar" in payload.get("extensions", {})
        assert payload["extensions"]["bazaar"]["info"]["output"]["example"]["verdict"]

    def test_verified_route_402_carries_bazaar_extension(self, client, monkeypatch):
        # verified_route must expose extensions.bazaar on its live 402 too, or it
        # stays stuck in Bazaar 'processing' — the exact failure from session_create.
        import base64, json
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post("/tools/verified_route/call",
                        json={"parameters": {"need": "dex pair liquidity"}})
        assert r.status_code == 402
        header = r.headers.get("PAYMENT-REQUIRED")
        assert header, "PAYMENT-REQUIRED header missing"
        payload = json.loads(base64.b64decode(header + "=" * (-len(header) % 4)))
        assert payload["resource"]["serviceName"] == "AgentPay x402 Trust Oracle"
        assert "trust-oracle" in payload["resource"]["tags"]
        assert "bazaar" in payload.get("extensions", {})
        assert payload["extensions"]["bazaar"]["info"]["output"]["example"]["recommendation"]

    def test_free_tool_402_has_no_bazaar_block(self, client, monkeypatch):
        import base64, json
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post("/tools/token_price/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        h = r.headers["PAYMENT-REQUIRED"]
        payload = json.loads(base64.b64decode(h + "=" * (-len(h) % 4)))
        assert "extensions" not in payload


class TestCallToolGet:
    """Discovery crawlers probe with GET — must get the 402, not a 405."""

    def test_get_returns_402_with_bazaar_extension(self, client, monkeypatch):
        import base64, json
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = client.get("/tools/pre_trade_check/call")
        assert r.status_code == 402
        h = r.headers["PAYMENT-REQUIRED"]
        payload = json.loads(base64.b64decode(h + "=" * (-len(h) % 4)))
        assert payload["resource"]["serviceName"] == "AgentPay Pre-Trade Risk Check"
        assert "bazaar" in payload.get("extensions", {})

    def test_get_unknown_tool_404(self, client):
        r = client.get("/tools/not_a_tool/call")
        assert r.status_code == 404


# ── Pure X-PAYMENT v2 clients (Coinbase-for-Agents readiness) ────────────────
#
# Standards-pure x402 clients send the v2 payload in X-PAYMENT and nothing
# else. Before this fix the legacy Stellar parser rejected them with
# 'Invalid X-Payment header format' — no spec-compliant client could pay.

class TestPureV2Client:

    def _v2(self, payload_dict):
        import base64, json
        return base64.b64encode(json.dumps(payload_dict).encode()).decode()

    def test_x_payment_only_v2_routes_to_base(
        self, client, monkeypatch, patch_route_tool_response
    ):
        import gateway.routes.tools as rt

        async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
            return {"success": True, "tx_hash": "0x" + "a" * 64,
                    "payer": "0x" + "b" * 40, "network": "eip155:8453", "reason": "ok"}
        monkeypatch.setattr(rt.base_pay, "settle_base_payment", fake_settle)
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        # PAID tool: $0 tools now route v2 payloads to _settle_free_v2
        # (wall E fix), so base-settle routing is pinned on a priced tool.
        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000, "side": "long"}},
            headers={"X-PAYMENT": self._v2({"x402Version": 2, "payload": {"signature": "0xsig"}})},
        )
        assert r.status_code == 200
        assert r.json()["payment"]["network"] == "eip155:8453"

    def test_x_payment_only_mode_b_routes_to_base(
        self, client, monkeypatch, patch_route_tool_response
    ):
        import gateway.routes.tools as rt

        async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
            return {"success": True, "tx_hash": "0x" + "d" * 64,
                    "payer": "0x" + "b" * 40, "network": "eip155:8453", "reason": "ok"}
        monkeypatch.setattr(rt.base_pay, "settle_base_payment", fake_settle)
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000, "side": "long"}},
            headers={"X-PAYMENT": self._v2({"tx_hash": "0x" + "d" * 64, "payer": "0x" + "b" * 40})},
        )
        assert r.status_code == 200

    def test_garbage_x_payment_keeps_clear_legacy_error(self, client):
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}, "agent_address": "GAGENT"},
            headers={"X-Payment": "complete garbage, no structure"},
        )
        assert r.status_code == 402
        assert "Invalid X-Payment header format" in r.json()["reason"]

    def test_legacy_stellar_header_unaffected(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}, "agent_address": "GAGENT"},
            headers={"X-Payment": "tx_hash=abc,from=GAGENT,id=uuid-9"},
        )
        assert r.status_code == 200
        assert r.json()["payment"]["tx_hash"] == "abc"

    def test_session_route_accepts_pure_v2(self, client, monkeypatch):
        import gateway.routes.session as sess

        async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
            return {"success": True, "tx_hash": "0x" + "e" * 64,
                    "payer": "0x" + "b" * 40, "network": "eip155:8453", "reason": "ok"}
        monkeypatch.setattr(sess, "settle_base_payment", fake_settle, raising=False)
        monkeypatch.setattr(sess.base_pay, "settle_base_payment", fake_settle)
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post(
            "/v1/session/create",
            json={"max_spend": "0.10"},
            headers={"X-PAYMENT": self._v2({"x402Version": 2, "payload": {"signature": "0xsig"}})},
        )
        assert r.status_code == 200
        assert r.json().get("session_id")


# ── Wall E fix: standard x402 v2 payloads complete FREE ($0) tools ───────────
#
# Standards-pure clients can't speak the `free:<id>` X-Payment dialect. A
# well-formed v2 payload on a $0 tool must be accepted as the free proof
# WITHOUT any on-chain settlement attempt, with nonce-based replay protection
# and the normal payment lifecycle. See FUNNEL_FINDINGS_2026-07.md.

class TestFreeToolStandardV2:

    @staticmethod
    def _v2_payload(nonce: str, value: str = "0"):
        import base64, json
        return base64.b64encode(json.dumps({
            "x402Version": 2,
            "scheme":      "exact",
            "network":     "eip155:8453",
            "payload": {
                "signature": "0x" + "f" * 130,
                "authorization": {
                    "from":        "0x" + "1" * 40,
                    "to":          "0x" + "2" * 40,
                    "value":       value,
                    "nonce":       nonce,
                    "validAfter":  "0",
                    "validBefore": "9999999999",
                },
            },
        }).encode()).decode()

    @pytest.fixture
    def forbid_base_settle(self, monkeypatch):
        """settle_base_payment must NEVER be called for a $0 tool."""
        import gateway.routes.tools as rt

        async def explode(*a, **kw):  # pragma: no cover - failure signal
            raise AssertionError("settle_base_payment called for a FREE tool")
        monkeypatch.setattr(rt.base_pay, "settle_base_payment", explode)

    def test_standard_v2_payload_completes_free_tool(
        self, client, patch_route_tool_response, forbid_base_settle
    ):
        nonce = "0x" + "ab" * 32
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={"PAYMENT-SIGNATURE": self._v2_payload(nonce)},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["payment"]["tx_hash"].startswith("free:")
        assert body["payment"]["network"] == "eip155:8453"
        assert body["payment"]["amount_usdc"] == "0.000"
        assert body["result"]["mocked"] is True

    def test_v2_payload_in_x_payment_header_also_works(
        self, client, patch_route_tool_response, forbid_base_settle
    ):
        # Standards-pure clients send X-PAYMENT only; normalize_payment_headers
        # must route it into the free path for $0 tools.
        nonce = "0x" + "cd" * 32
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={"X-PAYMENT": self._v2_payload(nonce)},
        )
        assert r.status_code == 200
        assert r.json()["payment"]["tx_hash"] == f"free:{nonce}"

    def test_nonce_replay_rejected(
        self, client, patch_route_tool_response, forbid_base_settle
    ):
        nonce = "0x" + "ee" * 32
        payload = self._v2_payload(nonce)
        r1 = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={"PAYMENT-SIGNATURE": payload},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={"PAYMENT-SIGNATURE": payload},
        )
        assert r2.status_code == 402
        assert "replay" in r2.json()["reason"].lower()

    def test_payload_without_nonce_rejected_with_hint(
        self, client, patch_route_tool_response, forbid_base_settle
    ):
        import base64, json
        bare = base64.b64encode(json.dumps(
            {"x402Version": 2, "payload": {"signature": "0xfake"}}
        ).encode()).decode()
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}},
            headers={"PAYMENT-SIGNATURE": bare},
        )
        assert r.status_code == 402
        body = r.json()
        assert body["reason"] == "missing_authorization_nonce"
        assert "free" in body["hint"].lower()

    def test_sdk_free_dialect_still_works(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        # The proprietary free:<id> X-Payment flow (SDK + MCP) is untouched.
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}, "agent_address": "GAGENT"},
            headers={"X-Payment": "tx_hash=free:uuid-77,from=GAGENT,id=uuid-77"},
        )
        assert r.status_code == 200
        assert r.json()["payment"]["tx_hash"] == "free:uuid-77"

    def test_free_402_advertises_no_settlement_needed(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = client.post("/tools/token_price/call", json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        base_opt = r.json()["payment_options"]["base"]
        assert "FREE" in base_opt["instructions"]
        assert "without any on-chain settlement" in base_opt["instructions"]


class TestBodyAcceptsEntry:
    """GitHub issue #1: generic x402 payers read `accepts[]` from the 402 JSON
    body with the STANDARD field names (payTo / maxAmountRequired). The
    standard entry used to live only inside the base64 PAYMENT-REQUIRED
    header; the body carried non-standard payment_options names (pay_to,
    amount_atomic), so generic clients missed the valid Base path."""

    def test_402_body_has_standard_accepts_when_base_configured(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = client.post("/tools/pre_trade_check/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        body = r.json()
        assert isinstance(body.get("accepts"), list) and body["accepts"], \
            "402 body must carry a non-empty standard accepts[] when Base is configured"
        a = body["accepts"][0]
        # Standard x402 field names — NOT pay_to / amount_atomic
        assert a["payTo"] == "0x" + "c" * 40
        assert a["scheme"] == "exact"
        assert a["network"].startswith("eip155:")
        assert a["asset"].startswith("0x")
        # Both dialects carry the same atomic amount
        assert a["maxAmountRequired"] == a["amount"] == "10000"  # $0.01
        assert a["resource"].endswith("/tools/pre_trade_check/call")
        assert a["mimeType"] == "application/json"
        # payment_options untouched (backward compat)
        assert "base" in body["payment_options"]

    def test_402_body_accepts_empty_when_base_not_configured(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "")
        r = client.post("/tools/pre_trade_check/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        assert r.json()["accepts"] == []

    def test_free_tool_402_body_accepts_is_zero_amount(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = client.post("/tools/token_price/call", json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        a = r.json()["accepts"][0]
        assert a["maxAmountRequired"] == a["amount"] == "0"

    def test_session_create_402_body_has_standard_accepts(self, client, monkeypatch):
        import gateway.routes.session as sess
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = client.get("/v1/session/create")
        assert r.status_code == 402
        a = r.json()["accepts"][0]
        assert a["payTo"] == "0x" + "c" * 40
        assert a["maxAmountRequired"] == a["amount"] == "10000"  # $0.01
        assert a["network"].startswith("eip155:")


# ── Verified payer wins over declared agent_address (Base paths) ─────────────
#
# Real buyers were logged under docs-example addresses (0x742d35Cc…, 0x0000…0)
# because the self-declared body/header agent_address took priority over the
# settle result's verified payer. See FUNNEL_FINDINGS_2026-07.md Finding 3.

class TestVerifiedPayerWins:

    VERIFIED_PAYER = "0x" + "9c" * 20
    DECLARED_JUNK  = "0x742d35Cc6634C0532925a3b844Bc9e7595f42bE5"

    def _v2(self):
        import base64, json
        return base64.b64encode(json.dumps(
            {"x402Version": 2, "payload": {"signature": "0xsig"}}
        ).encode()).decode()

    @pytest.fixture
    def fake_base_settle(self, monkeypatch):
        import gateway.routes.tools as rt

        async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
            return {"success": True, "tx_hash": "0x" + "77" * 32,
                    "payer": self.VERIFIED_PAYER, "network": "eip155:8453",
                    "reason": "ok"}
        monkeypatch.setattr(rt.base_pay, "settle_base_payment", fake_settle)
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

    def test_declared_junk_does_not_override_verified_payer_on_paid_tool(
        self, client, patch_route_tool_response, fake_base_settle, monkeypatch
    ):
        # Capture what the lifecycle write records as agent_address.
        import gateway.routes.tools as rt
        recorded = {}

        async def spy_update(payment_id, state, **fields):
            if state == "payment_done":
                recorded.update(fields)
            return True
        monkeypatch.setattr(rt, "update_payment_log_state", spy_update)

        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000, "side": "long"},
                  "agent_address": self.DECLARED_JUNK},
            headers={"PAYMENT-SIGNATURE": self._v2()},
        )
        assert r.status_code == 200
        assert recorded.get("agent_address") == self.VERIFIED_PAYER

    def test_session_route_records_verified_payer(self, client, monkeypatch):
        import gateway.routes.session as sess

        async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
            return {"success": True, "tx_hash": "0x" + "88" * 32,
                    "payer": self.VERIFIED_PAYER, "network": "eip155:8453",
                    "reason": "ok"}
        monkeypatch.setattr(sess.base_pay, "settle_base_payment", fake_settle)
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post(
            "/v1/session/create",
            json={"max_spend": "0.10",
                  "agent_address": "0x0000000000000000000000000000000000000000"},
            headers={"PAYMENT-SIGNATURE": self._v2()},
        )
        assert r.status_code == 200
        assert r.json()["agent_address"] == self.VERIFIED_PAYER


# ── In-band upsell block on paid responses ───────────────────────────────────

class TestPaidResponseUpsell:

    def _v2(self):
        import base64, json
        return base64.b64encode(json.dumps(
            {"x402Version": 2, "payload": {"signature": "0xsig"}}
        ).encode()).decode()

    @pytest.fixture
    def fake_base_settle(self, monkeypatch):
        import gateway.routes.tools as rt

        async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
            return {"success": True, "tx_hash": "0x" + "55" * 32,
                    "payer": "0x" + "9c" * 20, "network": "eip155:8453",
                    "reason": "ok"}
        monkeypatch.setattr(rt.base_pay, "settle_base_payment", fake_settle)
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

    def test_paid_tool_response_carries_related_block(
        self, client, patch_route_tool_response, fake_base_settle
    ):
        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": {"symbol": "ETH", "size_usd": 1000, "side": "long"}},
            headers={"PAYMENT-SIGNATURE": self._v2()},
        )
        assert r.status_code == 200
        rel = r.json()["related"]
        assert "plan/estimate" in rel["hint"]
        names = {t["tool"] for t in rel["paid_tools"]}
        assert names == {"verified_route", "session_create"}

    def test_free_tool_response_has_no_related_block(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {"symbol": "ETH"}, "agent_address": "GAGENT"},
            headers={"X-Payment": "tx_hash=free:uuid-88,from=GAGENT,id=uuid-88"},
        )
        assert r.status_code == 200
        assert "related" not in r.json()

    def test_session_create_response_carries_related_block(
        self, client, monkeypatch
    ):
        import gateway.routes.session as sess

        async def fake_settle(sig_header, requirements, rpc_url="", **kwargs):
            return {"success": True, "tx_hash": "0x" + "66" * 32,
                    "payer": "0x" + "9c" * 20, "network": "eip155:8453",
                    "reason": "ok"}
        monkeypatch.setattr(sess.base_pay, "settle_base_payment", fake_settle)
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post(
            "/v1/session/create",
            json={"max_spend": "0.10"},
            headers={"PAYMENT-SIGNATURE": self._v2()},
        )
        assert r.status_code == 200
        rel = r.json()["related"]
        names = {t["tool"] for t in rel["paid_tools"]}
        assert names == {"verified_route", "pre_trade_check"}


# ── F3 + F6 (follow-up review 2026-07-20): KPI-integrity guards ──────────────

class TestRejectedPatchGuarded:
    """F3: the 'rejected' PATCH is keyed on the CALLER'S header-supplied
    payment id. Without an expected_state filter, anyone replaying
    X-Payment: tx_hash=garbage,from=X,id=<pid> for a completed payment
    flips that row payment_done → rejected with attacker-chosen
    error_reason — the same clobber class the intermediate 'verified'
    PATCH was guarded against."""

    def test_rejected_patch_carries_expected_state_guard(
        self, client, supabase_lifecycle_capture, patch_route_verify
    ):
        patch_route_verify("replay")
        first = client.post("/tools/token_price/call", json={"parameters": {}})
        payment_id = first.json()["payment_id"]

        r = client.post(
            "/tools/token_price/call",
            json={"parameters": {}},
            headers={
                "X-Payment": f"tx_hash=replayhash,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENT",
            },
        )
        assert r.status_code == 402
        rejected = [u for u in supabase_lifecycle_capture["update"]
                    if u["state"] == "rejected"]
        assert len(rejected) == 1
        # The guard: only a non-terminal row may be flipped to rejected.
        assert rejected[0].get("expected_state") == ("pending", "verified")


class TestGetProbeBooksNoPendingRow:
    """F6: GET /tools/{name}/call is the discovery-probe path (x402scout
    health-checks every 15 min). It must issue the 402 WITHOUT the
    fail-closed pending INSERT — mirroring session_create_probe —
    or every paid tool accrues perpetual phantom 402-abandonments
    (the AGE-52 pollution class) and a Supabase blip 503s crawlers."""

    def test_get_probe_skips_pending_insert(
        self, client, supabase_lifecycle_capture
    ):
        r = client.get("/tools/token_price/call")
        assert r.status_code == 402
        assert r.json()["payment_id"]                      # challenge intact
        assert supabase_lifecycle_capture["insert"] == []  # no phantom row

    def test_post_challenge_still_books_pending_row(
        self, client, supabase_lifecycle_capture
    ):
        """Real callers POST — the fail-closed INSERT must be unchanged for
        PAID tools. (Disk-IO fix 2026-08-04: $0 tools skip it — their 402
        volume lives in probe_rollup instead.)"""
        r = client.post("/tools/pre_trade_check/call", json={"parameters": {}})
        assert r.status_code == 402
        assert len(supabase_lifecycle_capture["insert"]) == 1


class TestEndpointSafetyCgnat:
    """F5 (follow-up review 2026-07-20): the flag enumeration missed
    100.64.0.0/10 (CGNAT, is_private=False) — the range Railway's own
    internal fabric rides on. The guard now requires ip.is_global."""

    def test_blocks_cgnat_range(self):
        from gateway.routes.tools import _endpoint_is_safe
        assert _endpoint_is_safe("https://100.64.0.1/x")[0] is False
        assert _endpoint_is_safe("https://100.127.255.254/x")[0] is False

    def test_hostname_resolving_cgnat_is_blocked(self, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(
            rt.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("100.64.0.7", 443))],
        )
        ok, why = rt._endpoint_is_safe("https://looks-public.example/tool")
        assert ok is False

    def test_public_still_allowed(self):
        from gateway.routes.tools import _endpoint_is_safe
        ok, why = _endpoint_is_safe("https://8.8.8.8/tool")
        assert ok is True, why


# ── Bare-body parameter folding ──────────────────────────────────────────────

class TestBareBodyParamsFold:
    """Some clients send tool arguments at the top level of the JSON body
    ({"symbol": "SOL"}) instead of nested under "parameters". Pydantic
    silently discarded those keys, so such calls ran on tool defaults with
    no error and no trace. The model now folds unrecognized top-level keys
    into `parameters` when it is empty, and the paid response echoes what
    the tool actually ran with."""

    # — model-level folding —

    def test_bare_body_folds_into_parameters(self):
        from gateway.routes.tools import ToolCallRequest
        req = ToolCallRequest(**{"symbol": "SOL", "size_usd": 500})
        assert req.parameters == {"symbol": "SOL", "size_usd": 500}

    def test_nested_body_unchanged(self):
        from gateway.routes.tools import ToolCallRequest
        req = ToolCallRequest(parameters={"symbol": "SOL"}, agent_address="0xabc")
        assert req.parameters == {"symbol": "SOL"}
        assert req.agent_address == "0xabc"

    def test_nested_nonempty_wins_over_top_level_extras(self):
        """Canonical shape takes precedence — extras are NOT merged in."""
        from gateway.routes.tools import ToolCallRequest
        req = ToolCallRequest(**{"parameters": {"a": 1}, "b": 2})
        assert req.parameters == {"a": 1}

    def test_explicit_empty_parameters_still_folds(self):
        from gateway.routes.tools import ToolCallRequest
        req = ToolCallRequest(**{"parameters": {}, "symbol": "SOL"})
        assert req.parameters == {"symbol": "SOL"}

    def test_agent_address_never_folded(self):
        from gateway.routes.tools import ToolCallRequest
        req = ToolCallRequest(**{"agent_address": "GABC", "symbol": "SOL"})
        assert req.parameters == {"symbol": "SOL"}
        assert req.agent_address == "GABC"

    def test_empty_body_stays_empty(self):
        from gateway.routes.tools import ToolCallRequest
        req = ToolCallRequest()
        assert req.parameters == {}

    def test_non_dict_parameters_still_rejected(self):
        import pydantic
        from gateway.routes.tools import ToolCallRequest
        with pytest.raises(pydantic.ValidationError):
            ToolCallRequest(parameters="not a dict")

    # — end-to-end through the paid POST path —

    def _pay(self, client, body):
        first = client.post("/tools/token_price/call", json=body)
        assert first.status_code == 402
        payment_id = first.json()["payment_id"]
        r = client.post(
            "/tools/token_price/call",
            json=body,
            headers={
                "X-Payment": f"tx_hash=mocktxhash,from=GAGENT,id={payment_id}",
                "X-Agent-Address": "GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENT",
            },
        )
        assert r.status_code == 200
        return r.json()

    def test_bare_body_reaches_the_tool(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        body = self._pay(client, {"symbol": "SOL"})
        # the mocked executor echoes the params it received
        assert body["result"]["params"] == {"symbol": "SOL"}
        assert body["parameters_received"] == {"symbol": "SOL"}
        assert "parameters_note" not in body

    def test_nested_body_reaches_the_tool_unchanged(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        body = self._pay(client, {"parameters": {"symbol": "BTC"}})
        assert body["result"]["params"] == {"symbol": "BTC"}
        assert body["parameters_received"] == {"symbol": "BTC"}
        assert "parameters_note" not in body

    def test_empty_body_gets_defaults_note(
        self, client, patch_route_verify, patch_route_tool_response
    ):
        body = self._pay(client, {})
        assert body["parameters_received"] == {}
        assert "defaults" in body["parameters_note"]
