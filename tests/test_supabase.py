"""
test_supabase.py — Unit tests for gateway/services/supabase.py.

Mocks every Supabase REST endpoint with respx — no real network calls.
Live integration was already verified out-of-band (see PR #13 conversation),
where every function round-tripped against the real Supabase tables. These
tests pin the *behavior* — what the function returns under various Supabase
response shapes (happy path, conflict, 5xx, network failure, disabled).

Coverage:
    Replay protection
        record_payment_id (happy + duplicate)
        record_tx_hash (happy + composite-PK independence)
    Pending challenges
        store_pending_challenge (insert + datetime conversion)
        get_pending_challenge (server-side expires_at filter)
    Faucet IP cooldown
        record_faucet_ip (UPSERT)
    payment_logs lifecycle
        insert_pending_payment_log (returns inserted id)
        update_payment_log_state (PATCH semantics)
    Cross-cutting
        sb_enabled() False → all functions no-op
        Supabase 5xx → log error, don't raise
"""

import time
import uuid

import httpx
import pytest
import respx

from gateway.services.supabase import (
    cleanup_expired_challenges,
    delete_pending_challenge,
    faucet_ip_seen_recently,
    get_pending_challenge,
    insert_pending_payment_log,
    is_payment_id_consumed,
    is_tx_hash_consumed,
    record_faucet_ip,
    record_payment_id,
    record_tx_hash,
    store_pending_challenge,
    sweep_abandoned_pending,
    update_payment_log_state,
)


# ── Fixture: stub settings so Supabase calls go to a fake URL we mock ───────

@pytest.fixture(autouse=True)
def stub_supabase_settings(monkeypatch):
    """Force Supabase config so tests exercise the network path uniformly.
    Combined with respx, this stays hermetic — no real HTTP traffic."""
    import gateway.services.supabase as sb_module
    from gateway.config import get_settings

    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_test_key")
    get_settings.cache_clear()
    new_settings = get_settings()
    monkeypatch.setattr(sb_module, "settings", new_settings)
    yield new_settings
    get_settings.cache_clear()


SB = "https://test.supabase.co"


# ── Replay protection ───────────────────────────────────────────────────────

class TestReplayProtection:

    @pytest.mark.asyncio
    async def test_record_payment_id_happy_path(self):
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_payment_ids").mock(
                return_value=httpx.Response(201)
            )
            ok = await record_payment_id("test-uuid-1")
        assert ok is True

    @pytest.mark.asyncio
    async def test_record_payment_id_returns_false_on_409(self):
        # Supabase returns 409 when the PK conflicts (already consumed).
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_payment_ids").mock(
                return_value=httpx.Response(
                    409, json={"code": "23505", "message": "duplicate key"}
                )
            )
            ok = await record_payment_id("test-uuid-1")
        assert ok is False

    @pytest.mark.asyncio
    async def test_record_payment_id_5xx_does_not_block(self):
        # On Supabase 5xx, return True so the gateway doesn't reject
        # legitimate payments due to infrastructure issues. The error gets
        # logged so the failure is visible.
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_payment_ids").mock(
                return_value=httpx.Response(503)
            )
            ok = await record_payment_id("test-uuid-1")
        assert ok is True  # don't block on infra

    @pytest.mark.asyncio
    async def test_is_payment_id_consumed(self):
        with respx.mock:
            # First call: returns one row → consumed
            respx.get(f"{SB}/rest/v1/replay_payment_ids").mock(
                return_value=httpx.Response(200, json=[{"payment_id": "x"}])
            )
            assert await is_payment_id_consumed("test-uuid-1") is True

            # Second call: empty → not consumed
            respx.get(f"{SB}/rest/v1/replay_payment_ids").mock(
                return_value=httpx.Response(200, json=[])
            )
            assert await is_payment_id_consumed("test-uuid-2") is False

    @pytest.mark.asyncio
    async def test_record_tx_hash_composite_pk(self):
        # Same hash on different networks should both succeed — the
        # composite PK (tx_hash, network) keeps them independent.
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_tx_hashes").mock(
                return_value=httpx.Response(201)
            )
            assert await record_tx_hash("hash1", "stellar-mainnet") is True
            assert await record_tx_hash("hash1", "stellar-testnet") is True
            # Same (hash, network) → 409
            respx.post(f"{SB}/rest/v1/replay_tx_hashes").mock(
                return_value=httpx.Response(409)
            )
            assert await record_tx_hash("hash1", "stellar-mainnet") is False


# ── Pending challenges ──────────────────────────────────────────────────────

class TestPendingChallenges:

    @pytest.mark.asyncio
    async def test_store_pending_challenge_converts_unix_to_iso(self):
        # The function takes Unix float and must convert to ISO 8601 for
        # Postgres timestamptz. Confirm the body actually contains an ISO
        # string by intercepting the request.
        captured = {}

        def capture_request(request):
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(201)

        with respx.mock:
            respx.post(f"{SB}/rest/v1/pending_challenges").mock(
                side_effect=capture_request
            )
            await store_pending_challenge(
                payment_id="test-uuid",
                tool_name="token_price",
                amount_usdc="0.001",
                gateway_address="GTEST",
                developer_address="",
                expires_at=1234567890.0,
                request_data={"symbol": "ETH"},
            )

        assert "expires_at" in captured["body"]
        # ISO 8601 UTC of 1234567890.0 → "2009-02-13T23:31:30+00:00"
        assert captured["body"]["expires_at"].startswith("2009-02-13T23:31:30")
        # Empty developer_address must serialize to None (not "")
        assert captured["body"]["developer_address"] is None
        # request_data is jsonb — passed as a dict, not a string
        assert captured["body"]["request_data"] == {"symbol": "ETH"}

    @pytest.mark.asyncio
    async def test_get_pending_challenge_filters_expired_server_side(self):
        # The function must include `expires_at=gt.<now>` as a query param
        # so Postgres filters expired rows server-side. Otherwise expired
        # challenges would leak through.
        captured = {}

        def capture_request(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, json=[{"payment_id": "x", "tool_name": "t"}])

        with respx.mock:
            respx.get(f"{SB}/rest/v1/pending_challenges").mock(
                side_effect=capture_request
            )
            row = await get_pending_challenge("test-uuid")

        assert row is not None
        # URL must contain the gt filter — payload param shape: expires_at=gt.<iso>
        assert "expires_at=gt." in captured["url"]
        # Must also filter by payment_id
        assert "payment_id=eq.test-uuid" in captured["url"]

    @pytest.mark.asyncio
    async def test_get_pending_challenge_returns_none_when_empty(self):
        with respx.mock:
            respx.get(f"{SB}/rest/v1/pending_challenges").mock(
                return_value=httpx.Response(200, json=[])
            )
            row = await get_pending_challenge("test-uuid")
        assert row is None

    @pytest.mark.asyncio
    async def test_delete_pending_challenge_idempotent(self):
        # Deleting a non-existent row should NOT raise.
        with respx.mock:
            # 204 = No Content (Supabase returns this even when 0 rows match)
            respx.delete(f"{SB}/rest/v1/pending_challenges").mock(
                return_value=httpx.Response(204)
            )
            await delete_pending_challenge("nonexistent-uuid")
        # Test passes if no exception was raised.


# ── Faucet IP cooldown ──────────────────────────────────────────────────────

class TestFaucetIpLog:

    @pytest.mark.asyncio
    async def test_record_faucet_ip_uses_upsert_preference(self):
        # The function must send Prefer: resolution=merge-duplicates so
        # Postgres ON CONFLICT (ip) DO UPDATE fires on duplicate inserts.
        captured = {}

        def capture_request(request):
            captured["headers"] = dict(request.headers)
            return httpx.Response(201)

        with respx.mock:
            respx.post(f"{SB}/rest/v1/faucet_ip_log").mock(
                side_effect=capture_request
            )
            await record_faucet_ip("192.0.2.42")

        # The Prefer header should contain merge-duplicates
        assert "merge-duplicates" in captured["headers"].get("prefer", "")

    @pytest.mark.asyncio
    async def test_faucet_ip_seen_recently_passes_cooldown_filter(self):
        captured = {}

        def capture_request(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, json=[{"ip": "192.0.2.42"}])

        with respx.mock:
            respx.get(f"{SB}/rest/v1/faucet_ip_log").mock(
                side_effect=capture_request
            )
            seen = await faucet_ip_seen_recently("192.0.2.42", 600)

        assert seen is True
        # Server-side time filter: last_used > now() - 600s
        assert "last_used=gt." in captured["url"]


# ── payment_logs lifecycle ──────────────────────────────────────────────────

class TestPaymentLogsLifecycle:

    @pytest.mark.asyncio
    async def test_insert_pending_payment_log_returns_id(self):
        with respx.mock:
            respx.post(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(201, json=[{"id": 999, "state": "pending"}])
            )
            row_id = await insert_pending_payment_log(
                payment_id="test-uuid",
                tool_name="token_price",
                network="stellar-testnet",
                amount_usdc="0.001",
            )
        assert row_id == 999

    @pytest.mark.asyncio
    async def test_insert_pending_payment_log_skips_None_optional_fields(self):
        # Optional fields (agent_address, tx_hash, etc.) shouldn't be
        # included in the request body when None — we don't want to
        # explicitly null Supabase column defaults.
        captured = {}

        def capture_request(request):
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json=[{"id": 1}])

        with respx.mock:
            respx.post(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await insert_pending_payment_log(
                payment_id="test-uuid",
                tool_name="token_price",
                network="stellar-testnet",
                amount_usdc="0.001",
                client_ip="192.0.2.99",
                # agent_address, tx_hash, developer_address, user_agent omitted
            )

        # Required fields present
        assert captured["body"]["payment_id"] == "test-uuid"
        assert captured["body"]["state"] == "pending"
        # Provided optional present
        assert captured["body"]["client_ip"] == "192.0.2.99"
        # Omitted optionals NOT in body
        assert "agent_address" not in captured["body"]
        assert "tx_hash" not in captured["body"]
        assert "developer_address" not in captured["body"]
        assert "user_agent" not in captured["body"]

    @pytest.mark.asyncio
    async def test_update_payment_log_state_uses_patch(self):
        # The function uses PATCH (not POST) so Postgres only updates the
        # specified columns. The trigger handles updated_at.
        captured = {}

        def capture_request(request):
            import json
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            captured["url"] = str(request.url)
            return httpx.Response(204)

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await update_payment_log_state(
                "test-uuid", "verified",
                agent_address="GAGENT",
                tx_hash="hash123",
            )

        assert captured["method"] == "PATCH"
        assert captured["body"]["state"] == "verified"
        assert captured["body"]["agent_address"] == "GAGENT"
        assert captured["body"]["tx_hash"] == "hash123"
        # Filter must target by payment_id
        assert "payment_id=eq.test-uuid" in captured["url"]

    @pytest.mark.asyncio
    async def test_update_payment_log_state_drops_None_fields(self):
        # Caller might pass field=None by mistake. We should NOT include
        # those in the PATCH body — that would null the column.
        captured = {}

        def capture_request(request):
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await update_payment_log_state(
                "test-uuid", "verified",
                agent_address="GAGENT",
                error_reason=None,        # should be skipped
                refund_tx_hash=None,      # should be skipped
            )

        assert captured["body"] == {"state": "verified", "agent_address": "GAGENT"}
        assert "error_reason" not in captured["body"]
        assert "refund_tx_hash" not in captured["body"]


# ── sweep_abandoned_pending (PR #14) ────────────────────────────────────────

class TestSweepAbandonedPending:

    @pytest.mark.asyncio
    async def test_sweep_returns_count_of_transitioned_rows(self):
        # Supabase returns the patched rows when return=representation is set.
        # The function counts those and returns the number.
        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(200, json=[
                    {"payment_id": "uuid-1", "state": "abandoned"},
                    {"payment_id": "uuid-2", "state": "abandoned"},
                    {"payment_id": "uuid-3", "state": "abandoned"},
                ])
            )
            n = await sweep_abandoned_pending()
        assert n == 3

    @pytest.mark.asyncio
    async def test_sweep_filters_state_eq_pending_and_old_created_at(self):
        # The WHERE clause must target ONLY rows where state='pending'
        # AND created_at < cutoff. If it patched any other state, a
        # 'payment_done' row could be reverted to 'abandoned' — silent
        # data corruption.
        captured = {}

        def capture_request(request):
            captured["url"]    = str(request.url)
            captured["method"] = request.method
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=[])

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await sweep_abandoned_pending()

        assert captured["method"] == "PATCH"
        # Targets only pending rows
        assert "state=eq.pending" in captured["url"]
        # Time filter present (precise timestamp varies by clock)
        assert "created_at=lt." in captured["url"]
        # The PATCH body sets state='abandoned'
        assert captured["body"] == {"state": "abandoned"}

    @pytest.mark.asyncio
    async def test_sweep_returns_zero_on_supabase_error(self):
        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(503)
            )
            n = await sweep_abandoned_pending()
        assert n == 0

    @pytest.mark.asyncio
    async def test_sweep_returns_zero_when_supabase_disabled(self, monkeypatch):
        import gateway.services.supabase as sb_module
        from gateway.config import get_settings

        monkeypatch.setenv("SUPABASE_URL", "")
        get_settings.cache_clear()
        monkeypatch.setattr(sb_module, "settings", get_settings())

        with respx.mock:
            # No mocks — any HTTP call would raise. Confirms the early
            # return at the sb_enabled() guard.
            assert await sweep_abandoned_pending() == 0

        get_settings.cache_clear()


# ── Cross-cutting behavior ──────────────────────────────────────────────────

class TestCrossCutting:

    @pytest.mark.asyncio
    async def test_disabled_supabase_makes_all_writes_noop(self, monkeypatch):
        # When SUPABASE_URL is empty, sb_enabled() returns False and
        # write functions early-return without making any HTTP call.
        import gateway.services.supabase as sb_module
        from gateway.config import get_settings

        monkeypatch.setenv("SUPABASE_URL", "")
        get_settings.cache_clear()
        monkeypatch.setattr(sb_module, "settings", get_settings())

        with respx.mock:
            # NO mocks set up — if any function tries to make an HTTP call,
            # respx will raise an unmatched-request error and the test fails.
            assert await record_payment_id("x") is True       # no-op success
            assert await record_tx_hash("x", "net") is True
            await store_pending_challenge(
                payment_id="x", tool_name="t", amount_usdc="0",
                gateway_address="g", developer_address="", expires_at=0,
                request_data={},
            )
            await record_faucet_ip("192.0.2.1")
            assert await insert_pending_payment_log(
                payment_id="x", tool_name="t", network="n", amount_usdc="0",
            ) is None
            await update_payment_log_state("x", "y")
            assert await cleanup_expired_challenges() == 0

        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_disabled_supabase_makes_reads_return_safe_defaults(self, monkeypatch):
        # When disabled, reads return False (assume not consumed) so the
        # gateway defaults to in-memory state. None for get_pending_challenge.
        import gateway.services.supabase as sb_module
        from gateway.config import get_settings

        monkeypatch.setenv("SUPABASE_URL", "")
        get_settings.cache_clear()
        monkeypatch.setattr(sb_module, "settings", get_settings())

        with respx.mock:
            assert await is_payment_id_consumed("x") is False
            assert await is_tx_hash_consumed("x", "net") is False
            assert await get_pending_challenge("x") is None
            assert await faucet_ip_seen_recently("ip", 600) is False

        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_network_error_logs_but_does_not_raise(self):
        # httpx raises ConnectError on network failure. Our functions
        # must catch + log + return a safe default.
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_payment_ids").mock(
                side_effect=httpx.ConnectError("dns fail")
            )
            ok = await record_payment_id("x")  # must not raise
        assert ok is True
