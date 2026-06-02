"""
test_split_retry.py — Tests for split_payment() retry + durable-failure.

split_payment() forwards the developer's 85% on every paid call. Before the
resilience work it was a single fire-and-forget submit, so any transient
failure silently lost the developer their cut. These tests pin the new
behaviour:

  - transient failure then success  → retries, returns success
  - permanent failure (all attempts) → returns failure AND stamps the
    payment_logs row via mark_split_failed for reconciliation

We mock the synchronous stellar_sdk surface (Keypair, TransactionBuilder,
Server) so no real Horizon/network is touched. asyncio.to_thread runs the
patched callables inline.
"""

from unittest.mock import MagicMock, patch, AsyncMock

import pytest

import gateway.stellar as stellar


def _patch_stellar_plumbing(submit_side_effect):
    """Return a context-manager stack patching the sync stellar_sdk bits.

    submit_side_effect: passed to the submit_transaction mock's side_effect
    (an exception to raise, a list to iterate, or a return-value function).
    """
    server = MagicMock()
    server.load_account.return_value = MagicMock()
    server.submit_transaction.side_effect = submit_side_effect

    # TransactionBuilder(...).append_payment_op(...).set_timeout(...).build()
    # → a mock tx whose .sign() is a no-op.
    builder = MagicMock()
    builder.append_payment_op.return_value = builder
    builder.set_timeout.return_value = builder
    builder.build.return_value = MagicMock()

    return server, builder


@pytest.mark.asyncio
async def test_split_retries_then_succeeds():
    # Fail twice (transient), succeed on the third attempt.
    server, builder = _patch_stellar_plumbing(
        submit_side_effect=[
            Exception("Horizon 504"),
            Exception("Horizon 504"),
            {"hash": "abc123"},
        ]
    )

    with patch.object(stellar, "get_server", return_value=server), \
         patch.object(stellar, "TransactionBuilder", return_value=builder), \
         patch.object(stellar.Keypair, "from_secret", return_value=MagicMock(public_key="GGATEWAY")), \
         patch.object(stellar, "get_usdc_asset", return_value=MagicMock()), \
         patch.object(stellar.settings, "SPLIT_MAX_RETRIES", 3), \
         patch.object(stellar.settings, "SPLIT_RETRY_BASE_DELAY", 0.0), \
         patch.object(stellar.settings, "GATEWAY_SECRET_KEY", "S" + "A" * 55):
        result = await stellar.split_payment(
            tool_developer_address="GDEV",
            total_amount_usdc="0.001",
            gateway_fee_percent=0.15,
        )

    assert result["success"] is True
    assert result["tx_hash"] == "abc123"
    # Three submit attempts (2 failures + 1 success)
    assert server.submit_transaction.call_count == 3


@pytest.mark.asyncio
async def test_split_exhaustion_marks_failed():
    # Every attempt fails → exhaustion path must mark_split_failed + return False.
    server, builder = _patch_stellar_plumbing(
        submit_side_effect=Exception("op_underfunded")
    )

    mark_failed = AsyncMock()
    with patch.object(stellar, "get_server", return_value=server), \
         patch.object(stellar, "TransactionBuilder", return_value=builder), \
         patch.object(stellar.Keypair, "from_secret", return_value=MagicMock(public_key="GGATEWAY")), \
         patch.object(stellar, "get_usdc_asset", return_value=MagicMock()), \
         patch.object(stellar.settings, "SPLIT_MAX_RETRIES", 2), \
         patch.object(stellar.settings, "SPLIT_RETRY_BASE_DELAY", 0.0), \
         patch.object(stellar.settings, "GATEWAY_SECRET_KEY", "S" + "A" * 55), \
         patch("gateway.services.supabase.mark_split_failed", mark_failed):
        result = await stellar.split_payment(
            tool_developer_address="GDEV",
            total_amount_usdc="0.001",
            gateway_fee_percent=0.15,
            payment_id="pid-123",
        )

    assert result["success"] is False
    assert "op_underfunded" in result["reason"]
    # 1 initial + 2 retries = 3 attempts
    assert server.submit_transaction.call_count == 3
    # Durable reconciliation marker recorded once, with the payment_id + reason
    mark_failed.assert_awaited_once()
    args, _ = mark_failed.call_args
    assert args[0] == "pid-123"
    assert "op_underfunded" in args[1]
