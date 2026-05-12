"""
test_x402.py — Pure-function tests for gateway/x402.py.

Covered:
  - parse_payment_header
      happy path, empty input, missing fields, empty values (the Tier 1
      bug that let "id=" with empty value through), malformed input.
  - issue_payment_challenge
      generates UUID payment_id, sets correct expiry, populates pending
      store, default vs custom TTL.
  - build_402_headers
      response shape: X-Payment-Required, X-Payment-Expires content-type.
  - get_pending_count
      counts active challenges, sweeps expired ones.

Out of scope for this file (covered in test_stellar.py once it lands):
  - verify_and_fulfill — calls verify_payment which hits Horizon.
"""

import time
import uuid
from unittest.mock import patch

import pytest

from gateway.x402 import (
    PaymentChallenge,
    build_402_headers,
    get_pending_count,
    issue_payment_challenge,
    parse_payment_header,
    verify_and_fulfill,
    _completed_payments,
    _pending_challenges,
)


# ── parse_payment_header ─────────────────────────────────────────────────────

class TestParsePaymentHeader:
    """The header parser is the first line of defense against malformed
    payment proofs and the Tier 1 empty-value bypass bug."""

    def test_happy_path(self):
        header = "tx_hash=abc123,from=GABC456,id=550e8400-e29b-41d4-a716-446655440000"
        result = parse_payment_header(header)
        assert result == {
            "tx_hash": "abc123",
            "from": "GABC456",
            "id": "550e8400-e29b-41d4-a716-446655440000",
        }

    def test_handles_extra_whitespace(self):
        # spaces around equals signs and after commas should be tolerated
        header = "tx_hash = abc123 , from = GABC , id = uuid-here"
        result = parse_payment_header(header)
        assert result["tx_hash"] == "abc123"
        assert result["id"] == "uuid-here"

    def test_field_order_does_not_matter(self):
        header = "id=uuid-here,from=GABC,tx_hash=hash-here"
        result = parse_payment_header(header)
        assert result["tx_hash"] == "hash-here"
        assert result["id"] == "uuid-here"

    def test_returns_none_on_empty_header(self):
        assert parse_payment_header("") is None
        assert parse_payment_header(None) is None

    def test_returns_none_when_tx_hash_missing(self):
        header = "from=GABC,id=uuid"
        assert parse_payment_header(header) is None

    def test_returns_none_when_id_missing(self):
        header = "tx_hash=abc123,from=GABC"
        assert parse_payment_header(header) is None

    def test_returns_none_when_tx_hash_empty(self):
        # Tier 1 bug regression: the old parser accepted "tx_hash=" and
        # added empty string to _completed_payments. Confirm the empty-
        # value rejection is still in place.
        header = "tx_hash=,from=GABC,id=uuid"
        assert parse_payment_header(header) is None

    def test_returns_none_when_id_empty(self):
        # Same regression check for id= with empty value.
        header = "tx_hash=abc123,from=GABC,id="
        assert parse_payment_header(header) is None

    def test_returns_none_when_both_empty(self):
        header = "tx_hash=,id=,from=GABC"
        assert parse_payment_header(header) is None

    def test_handles_extra_unknown_fields(self):
        # Forward compatibility — adding a field shouldn't break parsing
        header = "tx_hash=abc,from=G,id=uuid,future_field=value"
        result = parse_payment_header(header)
        assert result["tx_hash"] == "abc"
        assert result["future_field"] == "value"


# ── issue_payment_challenge ──────────────────────────────────────────────────

class TestIssuePaymentChallenge:

    def test_returns_payment_challenge_dataclass(self, mock_settings):
        challenge = issue_payment_challenge(
            tool_name="token_price",
            price_usdc="0.001",
            developer_address="GDEV...",
            request_data={"parameters": {"symbol": "ETH"}},
        )
        assert isinstance(challenge, PaymentChallenge)
        assert challenge.tool_name == "token_price"
        assert challenge.amount_usdc == "0.001"
        assert challenge.developer_address == "GDEV..."

    def test_payment_id_is_valid_uuid(self, mock_settings):
        challenge = issue_payment_challenge("tool", "0.001", "GDEV", {})
        # Should not raise
        parsed = uuid.UUID(challenge.payment_id)
        assert str(parsed) == challenge.payment_id

    def test_uses_settings_gateway_address(self, mock_settings):
        challenge = issue_payment_challenge("tool", "0.001", "GDEV", {})
        # mock_settings sets GATEWAY_PUBLIC_KEY to a known test value
        assert challenge.gateway_address == mock_settings.GATEWAY_PUBLIC_KEY

    def test_default_ttl_is_120_seconds(self, mock_settings):
        before = time.time()
        challenge = issue_payment_challenge("tool", "0.001", "GDEV", {})
        # Allow ±2s wall-clock slack for slow CI
        assert 118 < (challenge.expires_at - before) < 122

    def test_custom_ttl_respected(self, mock_settings):
        before = time.time()
        challenge = issue_payment_challenge(
            "tool", "0.001", "GDEV", {}, ttl_seconds=300
        )
        assert 298 < (challenge.expires_at - before) < 302

    def test_challenge_is_added_to_pending_store(self, mock_settings):
        challenge = issue_payment_challenge("tool", "0.001", "GDEV", {})
        assert challenge.payment_id in _pending_challenges
        stored = _pending_challenges[challenge.payment_id]
        assert stored["tool_name"] == "tool"
        assert stored["amount_usdc"] == "0.001"

    def test_each_call_produces_unique_payment_id(self, mock_settings):
        ids = {
            issue_payment_challenge("tool", "0.001", "GDEV", {}).payment_id
            for _ in range(10)
        }
        assert len(ids) == 10

    def test_request_data_is_preserved(self, mock_settings):
        request_data = {"parameters": {"symbol": "ETH", "vs_currency": "USD"}}
        challenge = issue_payment_challenge("tool", "0.001", "GDEV", request_data)
        stored = _pending_challenges[challenge.payment_id]
        assert stored["request_data"] == request_data


# ── build_402_headers ────────────────────────────────────────────────────────

class TestBuild402Headers:

    def test_includes_all_required_fields(self):
        challenge = PaymentChallenge(
            payment_id="550e8400-e29b-41d4-a716-446655440000",
            tool_name="token_price",
            amount_usdc="0.001",
            gateway_address="GTEST",
            developer_address="GDEV",
            issued_at=time.time(),
            expires_at=time.time() + 120,
            request_data={},
        )
        headers = build_402_headers(challenge)
        assert "X-Payment-Required" in headers
        assert "X-Payment-Expires" in headers
        assert headers["Content-Type"] == "application/json"

    def test_payment_required_header_includes_all_fields(self):
        challenge = PaymentChallenge(
            payment_id="my-payment-id",
            tool_name="token_price",
            amount_usdc="0.005",
            gateway_address="GMYGATEWAY",
            developer_address="GDEV",
            issued_at=0,
            expires_at=120,
            request_data={},
        )
        h = build_402_headers(challenge)
        assert "version=1" in h["X-Payment-Required"]
        assert "network=stellar" in h["X-Payment-Required"]
        assert "address=GMYGATEWAY" in h["X-Payment-Required"]
        assert "amount=0.005" in h["X-Payment-Required"]
        assert "asset=USDC" in h["X-Payment-Required"]
        assert "id=my-payment-id" in h["X-Payment-Required"]


# ── get_pending_count ────────────────────────────────────────────────────────

class TestGetPendingCount:

    def test_zero_when_empty(self):
        assert get_pending_count() == 0

    def test_counts_active_challenges(self, mock_settings):
        for _ in range(3):
            issue_payment_challenge("tool", "0.001", "GDEV", {})
        assert get_pending_count() == 3

    def test_sweeps_expired_challenges(self, mock_settings):
        # Issue 2 challenges, then forcibly expire one of them by editing
        # the in-memory dict. get_pending_count should clean it up and
        # return 1.
        c1 = issue_payment_challenge("tool", "0.001", "GDEV", {})
        c2 = issue_payment_challenge("tool", "0.001", "GDEV", {})
        _pending_challenges[c1.payment_id]["expires_at"] = time.time() - 1
        assert get_pending_count() == 1
        # The expired one was actually removed, not just hidden from count
        assert c1.payment_id not in _pending_challenges
        assert c2.payment_id in _pending_challenges


# ── verify_and_fulfill ───────────────────────────────────────────────────────
#
# Regression coverage for the post-verify branches of verify_and_fulfill.
# Previously, test_routes_tools.py monkey-patched verify_and_fulfill out at
# the routes layer, and test_x402.py only exercised parser + challenge
# issuance — so the actual function body never ran in CI. PR #13b added
# three asyncio.create_task lines (replay dual-writes) and PR #13c had to
# hotfix an UnboundLocalError caused by a leftover local `import asyncio`
# inside the split_payment branch shadowing the module import. CI didn't
# catch it; agent/week2_test.py against gateway-testnet did, after deploy.
#
# These tests close that gap. They mock verify_payment + split_payment +
# Supabase writes to no-ops, but DO NOT touch asyncio.create_task — so any
# UnboundLocalError-class scoping bug in the success path surfaces here.

class TestVerifyAndFulfill:

    @pytest.fixture
    def patched_x402(self, monkeypatch, mock_settings):
        """Mock the I/O-touching dependencies of verify_and_fulfill.

        verify_payment → Horizon, split_payment → Horizon, sb.* → Supabase.
        We replace each with an awaitable no-op that records the call,
        leaving the asyncio.create_task wrappers in production code intact.
        Returns a dict of counters the test can assert on.
        """
        import gateway.x402 as x402_mod
        called = {
            "verify_payment": 0,
            "split_payment": 0,
            "record_payment_id": 0,
            "record_tx_hash": 0,
            "delete_pending_challenge": 0,
            "store_pending_challenge": 0,
        }

        async def fake_verify_payment(**kwargs):
            called["verify_payment"] += 1
            return {"verified": True, "reason": "ok"}

        async def fake_split_payment(**kwargs):
            called["split_payment"] += 1
            return {"success": True}

        async def _make_recorder(name, return_value=None):
            called[name] += 1
            return return_value

        # asyncio.create_task wraps these; the production code path runs
        # in full. Each fake is async to match the real signatures.
        async def fake_record_payment_id(payment_id):
            called["record_payment_id"] += 1
            return True

        async def fake_record_tx_hash(tx_hash, network):
            called["record_tx_hash"] += 1
            return True

        async def fake_delete_pending_challenge(payment_id):
            called["delete_pending_challenge"] += 1

        async def fake_store_pending_challenge(**kwargs):
            called["store_pending_challenge"] += 1

        monkeypatch.setattr(x402_mod, "verify_payment", fake_verify_payment)
        monkeypatch.setattr(x402_mod, "split_payment", fake_split_payment)
        monkeypatch.setattr(x402_mod.sb, "record_payment_id", fake_record_payment_id)
        monkeypatch.setattr(x402_mod.sb, "record_tx_hash", fake_record_tx_hash)
        monkeypatch.setattr(
            x402_mod.sb, "delete_pending_challenge", fake_delete_pending_challenge
        )
        monkeypatch.setattr(
            x402_mod.sb, "store_pending_challenge", fake_store_pending_challenge
        )

        return called

    @pytest.mark.asyncio
    async def test_success_path_with_third_party_developer(self, patched_x402, mock_settings):
        """Happy path with developer_address != gateway address.

        Forces verify_and_fulfill through BOTH branches:
          - top: 3 asyncio.create_task calls (replay dual-writes)
          - bottom: split_payment branch (which historically had a local
                    `import asyncio` that shadowed the module import)

        This is the exact configuration that produced the PR #13c
        UnboundLocalError on every paid call in production. If anyone
        re-introduces an inner `import asyncio` (or any other name-shadow
        that silently turns a module-level binding into a function-local),
        this test fails immediately.
        """
        # Different from mock_settings.GATEWAY_PUBLIC_KEY → enters the
        # split branch.
        third_party_dev = "GDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDEVDE"

        challenge = issue_payment_challenge(
            tool_name="token_price",
            price_usdc="0.001",
            developer_address=third_party_dev,
            request_data={"parameters": {"symbol": "ETH"}},
        )

        proof = (
            f"tx_hash=abc123def456,"
            f"from=GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGEN,"
            f"id={challenge.payment_id}"
        )
        result = await verify_and_fulfill(
            payment_header=proof,
            agent_address="GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGEN",
        )

        assert result["authorized"] is True
        assert result["tx_hash"] == "abc123def456"
        assert result["challenge"]["tool_name"] == "token_price"

        # In-memory side-effects
        assert "abc123def456" in _completed_payments
        assert challenge.payment_id not in _pending_challenges

        # Production stellar dependency was called
        assert patched_x402["verify_payment"] == 1

        # Yield once so the create_task'd coroutines actually run.
        # Without this, the fake sb.* counters can read 0 because the tasks
        # haven't been scheduled past their first suspension point yet.
        import asyncio
        for _ in range(5):
            await asyncio.sleep(0)

        # All three replay dual-writes fired (the line that crashed in
        # PR #13b before the hotfix).
        assert patched_x402["record_payment_id"] == 1
        assert patched_x402["record_tx_hash"] == 1
        assert patched_x402["delete_pending_challenge"] == 1
        # Split branch fired since developer != gateway.
        assert patched_x402["split_payment"] == 1

    @pytest.mark.asyncio
    async def test_success_path_skips_split_for_self_owned_tool(
        self, patched_x402, mock_settings
    ):
        """When developer_address == gateway address, skip the split.

        The replay dual-writes still fire (they're unconditional). This
        path used to be the common case before any third-party tools
        were registered — kept here so it doesn't regress.
        """
        challenge = issue_payment_challenge(
            tool_name="token_price",
            price_usdc="0.001",
            developer_address=mock_settings.GATEWAY_PUBLIC_KEY,  # ← same
            request_data={"parameters": {"symbol": "ETH"}},
        )

        proof = (
            f"tx_hash=hash999,"
            f"from=GAGENT,"
            f"id={challenge.payment_id}"
        )
        result = await verify_and_fulfill(
            payment_header=proof,
            agent_address="GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGEN",
        )

        assert result["authorized"] is True
        import asyncio
        for _ in range(5):
            await asyncio.sleep(0)

        # Replay dual-writes still fire
        assert patched_x402["record_payment_id"] == 1
        assert patched_x402["record_tx_hash"] == 1
        assert patched_x402["delete_pending_challenge"] == 1
        # Split branch correctly skipped
        assert patched_x402["split_payment"] == 0

    @pytest.mark.asyncio
    async def test_replay_attack_short_circuits(self, patched_x402, mock_settings):
        """Replay protection runs BEFORE verify_payment, so the dual-write
        side-effects never fire on a replay. This pins the order: free
        Stellar verifier round-trips for known-bad tx hashes, no Supabase
        writes for replay attempts."""
        challenge = issue_payment_challenge("token_price", "0.001", "GDEV", {})
        # Pre-seed _completed_payments to simulate a tx that was already used
        _completed_payments.add("already_used_hash")

        proof = (
            f"tx_hash=already_used_hash,"
            f"from=GAGENT,"
            f"id={challenge.payment_id}"
        )
        result = await verify_and_fulfill(
            payment_header=proof,
            agent_address="GAGENT",
        )

        assert result["authorized"] is False
        assert "replay" in result["reason"].lower()
        # No I/O fired — verify_payment never called, no replay writes
        assert patched_x402["verify_payment"] == 0
        assert patched_x402["record_payment_id"] == 0
        assert patched_x402["record_tx_hash"] == 0

    @pytest.mark.asyncio
    async def test_supabase_hit_for_pending_challenge(self, patched_x402, monkeypatch, mock_settings):
        """When Supabase is enabled and has the challenge, the dual-read
        should hit Supabase, NOT the in-memory dict.

        Forces sb_enabled True + a hit on get_pending_challenge.
        Deliberately leaves _pending_challenges empty — the only way the
        request succeeds is if the Supabase path is actually taken.
        """
        import gateway.x402 as x402_mod

        # Force Supabase ON for this test
        monkeypatch.setattr(x402_mod.sb, "sb_enabled", lambda: True)

        # Build a fake Supabase row matching the schema in pr-13-schema.sql
        payment_id = "supabase-served-uuid"
        future_iso = (
            __import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ) + __import__("datetime").timedelta(seconds=60)
        ).isoformat()
        async def fake_get(pid):
            assert pid == payment_id
            return {
                "payment_id":      payment_id,
                "tool_name":       "token_price",
                "amount_usdc":     "0.001",
                "gateway_address": mock_settings.GATEWAY_PUBLIC_KEY,
                "developer_address": None,
                "request_data":    {},
                "expires_at":      future_iso,
            }
        async def fake_not_consumed(*args, **kwargs):
            return False
        monkeypatch.setattr(x402_mod.sb, "get_pending_challenge", fake_get)
        monkeypatch.setattr(x402_mod.sb, "is_payment_id_consumed", fake_not_consumed)
        monkeypatch.setattr(x402_mod.sb, "is_tx_hash_consumed", fake_not_consumed)

        # Confirm the in-memory dict is empty — this proves the Supabase
        # path is what made the request succeed.
        assert payment_id not in _pending_challenges

        proof = f"tx_hash=hash_from_supabase,from=GAGENT,id={payment_id}"
        result = await verify_and_fulfill(
            payment_header=proof, agent_address="GAGENT"
        )
        assert result["authorized"] is True
        assert result["challenge"]["tool_name"] == "token_price"

    @pytest.mark.asyncio
    async def test_supabase_miss_falls_through_to_in_memory(
        self, patched_x402, monkeypatch, mock_settings
    ):
        """When Supabase is enabled but returns None, the dual-read must
        fall through to the in-memory dict. This is the drain-window
        case — challenges issued before a Supabase outage / before
        cutover still resolve from the local cache.
        """
        import gateway.x402 as x402_mod
        monkeypatch.setattr(x402_mod.sb, "sb_enabled", lambda: True)

        async def fake_get_none(pid):
            return None
        async def fake_not_consumed(*args, **kwargs):
            return False
        monkeypatch.setattr(x402_mod.sb, "get_pending_challenge", fake_get_none)
        monkeypatch.setattr(x402_mod.sb, "is_payment_id_consumed", fake_not_consumed)
        monkeypatch.setattr(x402_mod.sb, "is_tx_hash_consumed", fake_not_consumed)

        # Issue a challenge — lands in _pending_challenges (in-memory)
        challenge = issue_payment_challenge("token_price", "0.001", "GDEV", {})

        proof = f"tx_hash=fallbackhash,from=GAGENT,id={challenge.payment_id}"
        result = await verify_and_fulfill(
            payment_header=proof, agent_address="GAGENT"
        )
        assert result["authorized"] is True
        # Confirms the fallback path served the request

    @pytest.mark.asyncio
    async def test_supabase_replay_rejects_before_horizon(
        self, patched_x402, monkeypatch, mock_settings
    ):
        """If Supabase says the tx_hash or payment_id has already been
        consumed, reject as replay BEFORE calling verify_payment. This
        is the whole point of moving replay state to Supabase — the
        check survives a Railway redeploy.
        """
        import gateway.x402 as x402_mod
        monkeypatch.setattr(x402_mod.sb, "sb_enabled", lambda: True)

        # Issue a fresh challenge so the lookup succeeds
        challenge = issue_payment_challenge("token_price", "0.001", "GDEV", {})

        async def fake_get(pid):
            return None  # use in-memory
        async def fake_payment_id_consumed(pid):
            # Simulate Supabase saying "yes, this UUID was already used"
            return True
        async def fake_tx_hash_consumed(tx, net):
            return False
        monkeypatch.setattr(x402_mod.sb, "get_pending_challenge", fake_get)
        monkeypatch.setattr(x402_mod.sb, "is_payment_id_consumed", fake_payment_id_consumed)
        monkeypatch.setattr(x402_mod.sb, "is_tx_hash_consumed", fake_tx_hash_consumed)

        proof = f"tx_hash=brandnew_hash,from=GAGENT,id={challenge.payment_id}"
        result = await verify_and_fulfill(
            payment_header=proof, agent_address="GAGENT"
        )
        assert result["authorized"] is False
        assert "replay" in result["reason"].lower()
        # verify_payment was NEVER called — replay check short-circuits
        assert patched_x402["verify_payment"] == 0

    @pytest.mark.asyncio
    async def test_failed_verification_skips_dual_write(self, patched_x402, mock_settings):
        """When verify_payment returns verified=False, the dual-writes
        must NOT fire. Otherwise we'd record a tx_hash for a payment that
        the gateway never actually accepted, polluting replay_tx_hashes
        with bogus rows."""
        # Override the fake to return failure
        import gateway.x402 as x402_mod

        async def failing_verify(**kwargs):
            patched_x402["verify_payment"] += 1
            return {"verified": False, "reason": "amount_mismatch"}

        x402_mod.verify_payment = failing_verify

        challenge = issue_payment_challenge("token_price", "0.001", "GDEV", {})
        proof = (
            f"tx_hash=valid_looking_hash,"
            f"from=GAGENT,"
            f"id={challenge.payment_id}"
        )
        result = await verify_and_fulfill(
            payment_header=proof,
            agent_address="GAGENT",
        )

        assert result["authorized"] is False
        assert result["reason"] == "amount_mismatch"
        # verify_payment ran but no dual-writes
        assert patched_x402["verify_payment"] == 1
        assert patched_x402["record_payment_id"] == 0
        assert patched_x402["record_tx_hash"] == 0
        assert patched_x402["delete_pending_challenge"] == 0
        assert patched_x402["split_payment"] == 0
