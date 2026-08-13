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
    claim_refund_pending,
    cleanup_expired_challenges,
    correlate_pending_challenge,
    delete_pending_challenge,
    faucet_ip_seen_recently,
    get_pending_challenge,
    increment_refund_attempt,
    insert_pending_payment_log,
    is_payment_id_consumed,
    is_tx_hash_consumed,
    mark_refund_done,
    mark_refund_failed,
    persist_tool_registration,
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
    async def test_record_payment_id_5xx_fails_closed(self):
        """AGE-60: Supabase is the PRIMARY replay store (in-memory dies on
        every restart). A 5xx means the consume could NOT be confirmed —
        return None so callers reject retryably instead of authorizing a
        potentially-replayed payment."""
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_payment_ids").mock(
                return_value=httpx.Response(503)
            )
            ok = await record_payment_id("test-uuid-1")
        assert ok is None  # consume unconfirmed → caller must fail closed

    @pytest.mark.asyncio
    async def test_record_tx_hash_network_error_fails_closed(self):
        """AGE-60: same for transport-level failures on the tx-hash side."""
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_tx_hashes").mock(
                side_effect=httpx.ConnectError("supabase down")
            )
            ok = await record_tx_hash("hash-x", "stellar-mainnet")
        assert ok is None

    @pytest.mark.asyncio
    async def test_replay_store_sustained_failures_escalate(self, caplog):
        """AGE-60: 3+ consecutive record_* failures log CRITICAL so a broken
        table/RLS at cutover is loud, not silent."""
        import logging
        import gateway.services.supabase as sb_module
        sb_module._replay_store_consecutive_failures = 0
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_payment_ids").mock(
                return_value=httpx.Response(503)
            )
            with caplog.at_level(logging.ERROR):
                for _ in range(3):
                    assert await record_payment_id("uuid-n") is None
        assert any(r.levelno == logging.CRITICAL for r in caplog.records)
        # A success resets the streak.
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_payment_ids").mock(
                return_value=httpx.Response(201)
            )
            assert await record_payment_id("uuid-ok") is True
        assert sb_module._replay_store_consecutive_failures == 0

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

    # ── AGE-122: the mirror write retries once and reports honestly ────────

    @staticmethod
    def _challenge_kwargs(**over):
        kw = dict(
            payment_id="retry-uuid",
            tool_name="verified_route",
            amount_usdc="0.01",
            gateway_address="GTEST",
            developer_address="",
            expires_at=time.time() + 120,
            request_data={},
        )
        kw.update(over)
        return kw

    @pytest.mark.asyncio
    async def test_store_challenge_retries_once_after_timeout(self, monkeypatch, caplog):
        """AGE-122 shape: first attempt times out (the observed prod failure
        mode), the retry lands. No ERROR/WARNING, one INFO noting the retry."""
        import gateway.services.supabase as sb_module
        monkeypatch.setattr(sb_module, "_CHALLENGE_RETRY_DELAY", 0)
        calls = {"n": 0}

        def flaky(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(201)

        with respx.mock:
            respx.post(f"{SB}/rest/v1/pending_challenges").mock(side_effect=flaky)
            with caplog.at_level("INFO"):
                await store_pending_challenge(**self._challenge_kwargs())

        assert calls["n"] == 2
        assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]
        assert any("succeeded on retry" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_store_challenge_both_attempts_fail_warns_with_blast_radius(self, monkeypatch, caplog):
        """Final failure is a WARNING (not ERROR) and states what is actually
        lost — the durable mirror — so log readers don't treat it as an
        incident (AGE-122's original confusion)."""
        import gateway.services.supabase as sb_module
        monkeypatch.setattr(sb_module, "_CHALLENGE_RETRY_DELAY", 0)

        with respx.mock:
            respx.post(f"{SB}/rest/v1/pending_challenges").mock(
                side_effect=httpx.ConnectError("boom")
            )
            with caplog.at_level("INFO"):
                await store_pending_challenge(**self._challenge_kwargs())

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "after 2 attempts" in warnings[0].message
        assert "settleable in-memory" in warnings[0].message
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    @pytest.mark.asyncio
    async def test_store_challenge_409_duplicate_is_success(self, monkeypatch, caplog):
        """A 409 means the first attempt landed and only the response was
        lost — the mirror EXISTS. Must not retry-loop or log a failure."""
        import gateway.services.supabase as sb_module
        monkeypatch.setattr(sb_module, "_CHALLENGE_RETRY_DELAY", 0)
        route_calls = {"n": 0}

        def dup(request):
            route_calls["n"] += 1
            return httpx.Response(409, json={"code": "23505"})

        with respx.mock:
            respx.post(f"{SB}/rest/v1/pending_challenges").mock(side_effect=dup)
            with caplog.at_level("INFO"):
                await store_pending_challenge(**self._challenge_kwargs())

        assert route_calls["n"] == 1
        assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]

    @pytest.mark.asyncio
    async def test_store_challenge_http_500_retries_then_warns(self, monkeypatch, caplog):
        """Non-2xx/409 statuses go through the same retry-then-warn path as
        exceptions (the old code ERROR-logged 5xx without retrying)."""
        import gateway.services.supabase as sb_module
        monkeypatch.setattr(sb_module, "_CHALLENGE_RETRY_DELAY", 0)
        calls = {"n": 0}

        def failing(request):
            calls["n"] += 1
            return httpx.Response(500, text="upstream sad")

        with respx.mock:
            respx.post(f"{SB}/rest/v1/pending_challenges").mock(side_effect=failing)
            with caplog.at_level("INFO"):
                await store_pending_challenge(**self._challenge_kwargs())

        assert calls["n"] == 2
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "HTTP 500" in warnings[0].message

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
    async def test_update_payment_log_state_with_expected_state_adds_filter(self):
        """PR #14a regression: when expected_state is provided, the
        PATCH gains a `state=eq.<expected>` filter so the update only
        lands if the row's current state matches. This is the fix for
        the verified-after-payment_done race observed in the first
        post-#14 smoke test."""
        captured = {}

        def capture_request(request):
            captured["url"] = str(request.url)
            return httpx.Response(204)

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await update_payment_log_state(
                "test-uuid", "verified",
                expected_state="pending",
                agent_address="GAGENT",
            )

        # Both filters present: payment_id AND state
        assert "payment_id=eq.test-uuid" in captured["url"]
        assert "state=eq.pending" in captured["url"]

    @pytest.mark.asyncio
    async def test_update_payment_log_state_without_expected_state_unfiltered(self):
        """Inverse check: when expected_state is omitted (the terminal
        PATCH case), no state filter is added — the PATCH lands
        unconditionally on the row matching payment_id."""
        captured = {}

        def capture_request(request):
            captured["url"] = str(request.url)
            return httpx.Response(204)

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await update_payment_log_state(
                "test-uuid", "payment_done",
                agent_address="GAGENT",
            )

        assert "payment_id=eq.test-uuid" in captured["url"]
        assert "state=" not in captured["url"]

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


# ── Refund worker ORM (PR #12) ──────────────────────────────────────────────

class TestRefundORM:

    @pytest.mark.asyncio
    async def test_claim_refund_pending_filters_state_and_attempts(self):
        """The worker query must target ONLY rows in 'refund_pending'
        state with attempts < cap. Otherwise: refund_failed rows
        would be retried indefinitely, or already-done refunds would
        be re-sent."""
        captured = {}

        def capture_request(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, json=[])

        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await claim_refund_pending()

        assert "state=eq.refund_pending" in captured["url"]
        assert "refund_attempts=lt.5" in captured["url"]
        # Order matters for fair processing — oldest first
        assert "order=created_at.asc" in captured["url"]

    @pytest.mark.asyncio
    async def test_claim_refund_pending_returns_rows_for_worker(self):
        """The worker needs the actual row data (agent_address,
        amount_usdc, network) to construct the refund tx. Confirm
        the function returns the parsed JSON rather than swallowing it."""
        rows = [
            {"payment_id": "uuid-1", "agent_address": "GAGENT1",
             "amount_usdc": "0.002", "network": "stellar-testnet",
             "tool_name": "token_price", "refund_attempts": 0},
            {"payment_id": "uuid-2", "agent_address": "GAGENT2",
             "amount_usdc": "0.001", "network": "stellar-testnet",
             "tool_name": "gas_tracker", "refund_attempts": 2},
        ]
        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(200, json=rows)
            )
            result = await claim_refund_pending(limit=20)

        assert len(result) == 2
        assert result[0]["payment_id"] == "uuid-1"
        assert result[1]["refund_attempts"] == 2

    @pytest.mark.asyncio
    async def test_increment_refund_attempt_reads_then_writes_plus_one(self):
        """Read-modify-write semantics: read current count, write +1.
        With the single-worker invariant this is effectively atomic
        from the worker's perspective. The test pins that we always
        write current+1, not 1 (which would clobber).
        """
        write_payload = {}

        def get_handler(request):
            return httpx.Response(200, json=[{"refund_attempts": 3}])

        def patch_handler(request):
            import json
            write_payload.update(json.loads(request.content))
            return httpx.Response(204)

        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(side_effect=get_handler)
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(side_effect=patch_handler)
            new_count = await increment_refund_attempt("test-uuid")

        assert write_payload == {"refund_attempts": 4}
        assert new_count == 4  # AGE-61: confirmed new total returned

    @pytest.mark.asyncio
    async def test_increment_refund_attempt_read_blip_returns_none(self):
        """AGE-61: a failed read must NOT default to current=0 (which reset
        the counter from e.g. 4 back to 1 and let the worker blow past the
        5-attempt cap — each attempt a real USDC send). It returns None and
        performs NO write."""
        patched = {"called": False}

        def patch_handler(request):
            patched["called"] = True
            return httpx.Response(204)

        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(503)
            )
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(side_effect=patch_handler)
            assert await increment_refund_attempt("test-uuid") is None
        assert patched["called"] is False  # no write on a failed read

    @pytest.mark.asyncio
    async def test_increment_refund_attempt_missing_row_returns_none(self):
        """AGE-61: an empty read (row gone/filtered) is also a hard stop."""
        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(200, json=[])
            )
            assert await increment_refund_attempt("test-uuid") is None

    @pytest.mark.asyncio
    async def test_increment_refund_attempt_failed_write_returns_none(self):
        """AGE-61: an unconfirmed write must not authorize a send either."""
        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(200, json=[{"refund_attempts": 2}])
            )
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(500)
            )
            assert await increment_refund_attempt("test-uuid") is None

    @pytest.mark.asyncio
    async def test_mark_refund_done_uses_state_guard(self):
        """Terminal happy-path PATCH must filter to the in-flight states
        (refund_sending from AGE-76's two-phase claim, refund_pending for
        pre-deploy rows) so it can't accidentally overwrite a refund_failed
        row (which would happen if a delayed worker comes back to a row
        we'd already given up on)."""
        captured = {}

        def capture_request(request):
            captured["params"] = dict(request.url.params)
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await mark_refund_done("test-uuid", "refund_hash_abc")

        # State-guarded to in-flight states only
        assert captured["params"]["state"] == "in.(refund_sending,refund_pending)"
        # Carries the refund tx_hash
        assert captured["body"]["state"] == "refund_done"
        assert captured["body"]["refund_tx_hash"] == "refund_hash_abc"

    @pytest.mark.asyncio
    async def test_mark_refund_failed_idempotent_via_in_filter(self):
        """The terminal-sad PATCH includes the in-flight states
        ('refund_pending', 'refund_sending' — AGE-76 two-phase claim) AND
        'refund_failed' in its state filter so a second call to
        mark_refund_failed (e.g. a retry of the worker's give-up
        logic) lands as a no-op rather than a 0-row update. Otherwise
        callers can't distinguish 'already terminal' from 'PATCH bug'.
        """
        captured = {}

        def capture_request(request):
            captured["params"] = dict(request.url.params)
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(204)

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await mark_refund_failed("test-uuid", "max_attempts_exhausted")

        assert captured["params"]["state"] == \
            "in.(refund_pending,refund_sending,refund_failed)"
        assert captured["body"]["state"] == "refund_failed"
        assert captured["body"]["error_reason"] == "max_attempts_exhausted"


# ── correlate_pending_challenge (2026-07-17, phantom-abandon fix) ───────────

class TestCorrelatePendingChallenge:
    """x402-v2 doesn't echo our UUID, so a Base settle writes a tx-keyed row and
    the original 402 row is swept to 'abandoned' — every success booking a
    phantom abandonment. These pin the correlation that marks it 'superseded'.
    """

    @pytest.mark.asyncio
    async def test_marks_matching_pending_row_superseded(self):
        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(200, json=[{"payment_id": "uuid-42"}])
            )
            patched = {}

            def capture(request):
                import json
                patched["url"] = str(request.url)
                patched["body"] = json.loads(request.content)
                return httpx.Response(204)

            respx.patch(f"{SB}/rest/v1/payment_logs").mock(side_effect=capture)
            pid = await correlate_pending_challenge(
                tool_name="session_create", client_ip="100.64.0.5",
                user_agent="node", tx_hash="0xdead",
            )
        assert pid == "uuid-42"
        # 'superseded', NOT 'payment_done' — marking it done would double-count
        # the success in the very conversion query this exists to fix.
        assert patched["body"]["state"] == "superseded"
        assert patched["body"]["tx_hash"] == "0xdead"
        # Guard the PATCH: must be scoped to that row AND still-pending, so a
        # terminal row can never be clobbered by a late correlation.
        assert "payment_id=eq.uuid-42" in patched["url"]
        assert "state=eq.pending" in patched["url"]

    @pytest.mark.asyncio
    async def test_lookup_scoped_to_pending_rows_in_window(self):
        captured = {}

        def capture(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, json=[])

        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(side_effect=capture)
            res = await correlate_pending_challenge(
                tool_name="pre_trade_check", client_ip="100.64.0.5",
                user_agent="node", tx_hash="0xbeef",
            )
        assert res is None  # no candidate → nothing correlated
        assert "state=eq.pending" in captured["url"]
        assert "tool_name=eq.pre_trade_check" in captured["url"]
        assert "created_at=gte." in captured["url"]   # bounded by the sweep window
        assert "limit=1" in captured["url"]

    @pytest.mark.asyncio
    async def test_missing_ua_still_correlates_on_tool_and_window(self):
        # Genuine paid rows often carry user_agent=None. A UA-less client must
        # still correlate rather than not at all — otherwise the phantom stays.
        captured = {}

        def capture(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, json=[{"payment_id": "uuid-7"}])

        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(side_effect=capture)
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(204)
            )
            pid = await correlate_pending_challenge(
                tool_name="session_create", client_ip=None,
                user_agent=None, tx_hash="0xabc",
            )
        assert pid == "uuid-7"
        assert "user_agent" not in captured["url"]
        assert "client_ip" not in captured["url"]

    @pytest.mark.asyncio
    async def test_supabase_error_returns_none_and_does_not_raise(self):
        # The payment already settled ON-CHAIN before this runs. Analytics must
        # never raise into a settled call; failure degrades to the sweep.
        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(500, text="boom")
            )
            assert await correlate_pending_challenge(
                tool_name="session_create", client_ip=None,
                user_agent=None, tx_hash="0x1",
            ) is None

    @pytest.mark.asyncio
    async def test_network_failure_returns_none_and_does_not_raise(self):
        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=httpx.ConnectError("network down")
            )
            assert await correlate_pending_challenge(
                tool_name="session_create", client_ip=None,
                user_agent=None, tx_hash="0x1",
            ) is None

    @pytest.mark.asyncio
    async def test_disabled_supabase_is_noop(self, monkeypatch):
        import gateway.services.supabase as sb_module
        from gateway.config import get_settings

        monkeypatch.setenv("SUPABASE_URL", "")
        get_settings.cache_clear()
        monkeypatch.setattr(sb_module, "settings", get_settings())
        with respx.mock:
            # No mocks: any HTTP call here fails the test.
            assert await correlate_pending_challenge(
                tool_name="session_create", client_ip=None,
                user_agent=None, tx_hash="0x1",
            ) is None
        get_settings.cache_clear()


# ── sweep_abandoned_pending (PR #14) ────────────────────────────────────────

class TestSweepAbandonedPending:

    @pytest.mark.asyncio
    async def test_sweep_returns_count_from_content_range(self):
        # AGE-75: count=exact + return=minimal — the affected count comes from
        # the Content-Range header, NOT an echoed body of every row.
        seen = {}

        def handler(request):
            seen["prefer"] = request.headers.get("Prefer", "")
            return httpx.Response(204, headers={"Content-Range": "*/3"})

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(side_effect=handler)
            n = await sweep_abandoned_pending()
        assert n == 3
        assert "count=exact" in seen["prefer"]
        assert "return=minimal" in seen["prefer"]

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
        # must catch + log + return a safe value — for record_* that is
        # None (AGE-60 fail-closed: consume unconfirmed), never a raise.
        with respx.mock:
            respx.post(f"{SB}/rest/v1/replay_payment_ids").mock(
                side_effect=httpx.ConnectError("dns fail")
            )
            ok = await record_payment_id("x")  # must not raise
        assert ok is None


class TestAge60FollowUps:
    """Compensating rollback + cap-exhausted sweep (AGE-60/61 follow-ups)."""

    @pytest.mark.asyncio
    async def test_unrecord_tx_hash_deletes_by_composite_key(self):
        from gateway.services.supabase import unrecord_tx_hash
        seen = {}

        def delete_handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(204)

        with respx.mock:
            respx.delete(f"{SB}/rest/v1/replay_tx_hashes").mock(
                side_effect=delete_handler
            )
            assert await unrecord_tx_hash("deadbeef", "stellar-mainnet") is True
        assert seen["params"]["tx_hash"] == "eq.deadbeef"
        assert seen["params"]["network"] == "eq.stellar-mainnet"

    @pytest.mark.asyncio
    async def test_unrecord_tx_hash_failure_returns_false_logs_critical(self, caplog):
        import logging
        from gateway.services.supabase import unrecord_tx_hash
        with respx.mock:
            respx.delete(f"{SB}/rest/v1/replay_tx_hashes").mock(
                return_value=httpx.Response(500)
            )
            with caplog.at_level(logging.CRITICAL):
                assert await unrecord_tx_hash("deadbeef", "stellar-mainnet") is False
        assert any("half-consumed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_sweep_cap_exhausted_filters_and_counts(self):
        from gateway.services.supabase import (
            _REFUND_ATTEMPT_CAP,
            sweep_cap_exhausted_refunds,
        )
        seen = {}

        def patch_handler(request):
            import json
            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=[{"payment_id": "a"}, {"payment_id": "b"}])

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(side_effect=patch_handler)
            n = await sweep_cap_exhausted_refunds()
        assert n == 2
        assert seen["params"]["state"] == "eq.refund_pending"
        assert seen["params"]["refund_attempts"] == f"gte.{_REFUND_ATTEMPT_CAP}"
        assert seen["body"] == {"state": "refund_failed",
                                "error_reason": "cap_exhausted_no_send"}


class TestTwoPhaseRefundClaim:
    """AGE-76: claim_refund_sending / release_refund_sending / list_refund_sending."""

    @pytest.mark.asyncio
    async def test_claim_confirmed_only_on_exactly_one_row(self):
        from gateway.services.supabase import claim_refund_sending
        seen = {}

        def handler(request):
            import json
            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            seen["prefer"] = request.headers.get("Prefer", "")
            return httpx.Response(200, json=[{"payment_id": "p1"}])

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(side_effect=handler)
            assert await claim_refund_sending("p1") is True
        assert seen["params"]["state"] == "eq.refund_pending"   # guarded transition
        assert seen["body"] == {"state": "refund_sending"}
        assert "return=representation" in seen["prefer"]        # confirmation required

    @pytest.mark.asyncio
    async def test_claim_unconfirmed_on_zero_rows_or_error(self):
        from gateway.services.supabase import claim_refund_sending
        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                return_value=httpx.Response(200, json=[])   # row not in refund_pending
            )
            assert await claim_refund_sending("p1") is False
        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=httpx.ConnectError("down")
            )
            assert await claim_refund_sending("p1") is False   # no send on unknown

    @pytest.mark.asyncio
    async def test_release_is_state_guarded(self):
        from gateway.services.supabase import release_refund_sending
        seen = {}

        def handler(request):
            import json
            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            return httpx.Response(204)

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(side_effect=handler)
            await release_refund_sending("p1")
        assert seen["params"]["state"] == "eq.refund_sending"
        assert seen["body"]["state"] == "refund_pending"

    @pytest.mark.asyncio
    async def test_list_refund_sending_selects_worker_columns(self):
        from gateway.services.supabase import list_refund_sending
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[{"payment_id": "p1"}])

        with respx.mock:
            respx.get(f"{SB}/rest/v1/payment_logs").mock(side_effect=handler)
            rows = await list_refund_sending()
        assert rows == [{"payment_id": "p1"}]
        assert seen["params"]["state"] == "eq.refund_sending"


# ── Tool registry persistence (AGE-71) ───────────────────────────────────────

class TestPersistToolRegistration:
    """AGE-71: runtime tool registrations must be pushed to Supabase so they
    survive the next restart (hydration already merges them back at boot)."""

    def _tool(self, **overrides):
        base = {
            "name": "runtime_tool_abc",
            "description": "a developer-registered tool",
            "endpoint": "https://8.8.8.8/tool",
            "price_usdc": "0.001",
            "developer_address": "GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2",
            "parameters": {"type": "object", "properties": {}},
            "category": "data",
            "active": True,
            "uptime_pct": 100.0,
            "total_calls": 0,
            "triggers": [],
            "use_when": "",
            "returns": "",
            "response_example": None,
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_persist_upserts_and_returns_true(self):
        captured = {}

        def _capture(request):
            captured["prefer"] = request.headers.get("prefer")
            captured["body"] = request.content
            return httpx.Response(201)

        with respx.mock:
            respx.post(f"{SB}/rest/v1/tools").mock(side_effect=_capture)
            ok = await persist_tool_registration(self._tool())
        assert ok is True
        # Upsert semantics so re-registration converges instead of 409-ing.
        assert "merge-duplicates" in (captured["prefer"] or "")
        import json as _json
        assert _json.loads(captured["body"])["name"] == "runtime_tool_abc"

    @pytest.mark.asyncio
    async def test_persist_maps_empty_developer_address_to_null(self):
        captured = {}

        def _capture(request):
            import json as _json
            captured["dev"] = _json.loads(request.content)["developer_address"]
            return httpx.Response(201)

        with respx.mock:
            respx.post(f"{SB}/rest/v1/tools").mock(side_effect=_capture)
            await persist_tool_registration(self._tool(developer_address=""))
        assert captured["dev"] is None

    @pytest.mark.asyncio
    async def test_persist_returns_false_on_5xx(self):
        with respx.mock:
            respx.post(f"{SB}/rest/v1/tools").mock(
                return_value=httpx.Response(500, text="boom")
            )
            ok = await persist_tool_registration(self._tool())
        assert ok is False

    @pytest.mark.asyncio
    async def test_persist_returns_false_on_network_error(self):
        with respx.mock:
            respx.post(f"{SB}/rest/v1/tools").mock(
                side_effect=httpx.ConnectError("supabase down")
            )
            ok = await persist_tool_registration(self._tool())
        assert ok is False

    @pytest.mark.asyncio
    async def test_persist_noop_when_disabled(self, monkeypatch):
        import gateway.services.supabase as sb_module
        monkeypatch.setattr(sb_module, "sb_enabled", lambda: False)
        # No respx route registered — a network call would raise, proving
        # the disabled short-circuit returns before touching httpx.
        ok = await persist_tool_registration(self._tool())
        assert ok is False


class TestExpectedStateTupleFilter:
    """F3 (2026-07-20): expected_state accepts a tuple/list → PostgREST
    in.(...) filter, used by the guarded 'rejected' PATCH."""

    @pytest.mark.asyncio
    async def test_tuple_expected_state_builds_in_filter(self):
        from urllib.parse import unquote
        captured = {}

        def capture_request(request):
            captured["url"] = str(request.url)
            return httpx.Response(204)

        with respx.mock:
            respx.patch(f"{SB}/rest/v1/payment_logs").mock(
                side_effect=capture_request
            )
            await update_payment_log_state(
                "test-uuid", "rejected",
                expected_state=("pending", "verified"),
                error_reason="tx not found",
            )

        url = unquote(captured["url"])
        assert "payment_id=eq.test-uuid" in url
        assert "state=in.(pending,verified)" in url


class TestPaymentParamsObservability:
    """Buyer observability (2026-07-28): paid rows carry the request params
    (which symbols pre_trade_check screens, which needs verified_route vets)
    so the buyer-health digest can say WHAT was bought, not just how much.
    payment_logs is private and public reads select explicit columns."""

    @staticmethod
    def _capture():
        captured = {"bodies": []}

        def handler(request):
            import json as _json
            captured["bodies"].append(_json.loads(request.content))
            return httpx.Response(201, json=[{"id": 7}])
        return captured, handler

    @pytest.mark.asyncio
    async def test_parameters_included_when_passed(self):
        captured, handler = self._capture()
        with respx.mock:
            respx.post(f"{SB}/rest/v1/payment_logs").mock(side_effect=handler)
            await insert_pending_payment_log(
                payment_id="p1", tool_name="pre_trade_check",
                network="eip155:8453", amount_usdc="0.01",
                parameters={"symbol": "LINK", "size_usd": 25000, "side": "long"},
            )
        assert captured["bodies"][0]["parameters"] == {
            "symbol": "LINK", "size_usd": 25000, "side": "long"}

    @pytest.mark.asyncio
    async def test_parameters_omitted_when_none(self):
        captured, handler = self._capture()
        with respx.mock:
            respx.post(f"{SB}/rest/v1/payment_logs").mock(side_effect=handler)
            await insert_pending_payment_log(
                payment_id="p2", tool_name="token_price",
                network="stellar-testnet", amount_usdc="0.001")
        assert "parameters" not in captured["bodies"][0]

    @pytest.mark.asyncio
    async def test_oversized_parameters_become_a_marker(self):
        # A caller must not be able to bloat our rows with megabyte params.
        captured, handler = self._capture()
        with respx.mock:
            respx.post(f"{SB}/rest/v1/payment_logs").mock(side_effect=handler)
            await insert_pending_payment_log(
                payment_id="p3", tool_name="pre_trade_check",
                network="eip155:8453", amount_usdc="0.01",
                parameters={"blob": "x" * 5000})
        sent = captured["bodies"][0]["parameters"]
        assert sent["_truncated"] is True and sent["_bytes"] > 2048

    @pytest.mark.asyncio
    async def test_missing_column_degrades_and_row_still_lands(self):
        # Pre-migration: PostgREST 400s on the unknown column. The pre-402
        # caller FAILS CLOSED on None, so a schema gap must degrade to the
        # old shape — never into refused challenges.
        calls = {"n": 0}

        def handler(request):
            import json as _json
            calls["n"] += 1
            body = _json.loads(request.content)
            if "parameters" in body:
                return httpx.Response(
                    400, json={"message": "Could not find the 'parameters' column"})
            return httpx.Response(201, json=[{"id": 42}])

        with respx.mock:
            respx.post(f"{SB}/rest/v1/payment_logs").mock(side_effect=handler)
            row_id = await insert_pending_payment_log(
                payment_id="p4", tool_name="verified_route",
                network="eip155:8453", amount_usdc="0.01",
                parameters={"need": "dex pair liquidity"})
        assert row_id == 42
        assert calls["n"] == 2          # tried with, retried without


# ── Hydration seed-fallback for `endpoint` (external report, 2026-08-06) ──────

class TestHydrationEndpointFallback:
    """A Supabase tools row with an empty/null `endpoint` column must not
    blank the tool's discovery endpoint: registry.py is the source of truth
    for discovery fields, Supabase an override layer. Live regression found
    by the Circadian audit agent (2026-08-06, verified): gas_tracker,
    open_interest and orderbook_depth served endpoint="" because the seed
    fallback block repaired four fields but not `endpoint` — the one field a
    buyer can't reconstruct (session_create's /v1/session/create proves the
    path shape carries real information)."""

    @pytest.mark.asyncio
    async def test_empty_and_null_endpoints_fall_back_to_seed(self, monkeypatch):
        import gateway.main as main_module
        import gateway.services.supabase as sb_module
        from gateway.main import _hydrate_tools_from_supabase
        from registry import get_tool, list_tools, reload_tools
        from registry.registry import _TOOLS as _SEED

        # The autouse fixture stubs the SB settings onto the supabase service
        # module only; hydration reads gateway.main's settings — point it at
        # the same stub so the fetch actually hits the respx route.
        monkeypatch.setattr(main_module, "settings", sb_module.settings)

        snapshot = list(list_tools())
        rows = [
            # empty-string column (the live gas_tracker shape)
            {"name": "gas_tracker", "endpoint": "", "price_usdc": "0.001",
             "active": True},
            # null column (partial row)
            {"name": "open_interest", "endpoint": None, "price_usdc": "0.002",
             "active": True},
            # a real override must be RESPECTED, not clobbered by the seed
            {"name": "session_create",
             "endpoint": "https://override.example/session",
             "price_usdc": "0.01", "active": True},
        ]
        try:
            with respx.mock:
                route = respx.get(f"{SB}/rest/v1/tools").mock(
                    return_value=httpx.Response(200, json=rows))
                await _hydrate_tools_from_supabase()
            assert route.called          # hydration really ran against the mock
            assert get_tool("gas_tracker").endpoint == \
                _SEED["gas_tracker"].endpoint != ""
            assert get_tool("open_interest").endpoint == \
                _SEED["open_interest"].endpoint != ""
            # differs from the seed value, so this proves the override layer
            assert (get_tool("session_create").endpoint
                    == "https://override.example/session")
            # seed tools absent from Supabase still appended, with endpoints
            assert all(t.endpoint for t in list_tools() if t.active)
        finally:
            reload_tools(snapshot)

    @pytest.mark.asyncio
    async def test_startup_warning_names_tools_with_blank_fields(
            self, monkeypatch, caplog):
        """AGE-107 invariant: if a blank discovery field ever survives the
        merge again, the boot log must say so — silence is how this shipped."""
        import logging
        import gateway.main as main_module
        import gateway.services.supabase as sb_module
        from gateway.main import _hydrate_tools_from_supabase
        from registry import list_tools, reload_tools

        monkeypatch.setattr(main_module, "settings", sb_module.settings)
        snapshot = list(list_tools())
        # a runtime-registered tool with no seed to repair it: blank stays
        rows = [{"name": "runtime_orphan_tool", "endpoint": "",
                 "description": "an orphan", "price_usdc": "0.001",
                 "active": True}]
        try:
            with respx.mock:
                respx.get(f"{SB}/rest/v1/tools").mock(
                    return_value=httpx.Response(200, json=rows))
                with caplog.at_level(logging.WARNING):
                    await _hydrate_tools_from_supabase()
            assert any("DISCOVERY-CONTRACT" in r.message
                       and "runtime_orphan_tool" in r.message
                       for r in caplog.records)
        finally:
            reload_tools(snapshot)
