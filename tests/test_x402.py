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
