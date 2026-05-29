"""
test_agentpay_sdk.py — Tests for the agentpay-x402 SDK's retry-after-payment
parser. v0.1.4 adds RefundPending as a typed exception that callers can
catch, surfacing the gateway PR #12 contract (payment_status,
refund_eta_seconds, payment_id, error_reason) without forcing user code
to parse JSON.

The other parts of the SDK (AgentWallet, Session, _wallet helpers) are
exercised by the rest of the suite via the gateway integration tests.
This file is focused on the new parser path.

Mocks at the httpx layer with respx so we don't touch Stellar or the
gateway. The wallet itself is stubbed because we don't need real
on-chain signing for the parser tests.
"""

from unittest.mock import MagicMock

import httpx
import pytest
import respx

from agentpay._client import AgentPayClient
from agentpay._wallet import PaymentFailed, RefundPending


GATEWAY = "https://gateway-fake.example"
TOOL_URL = f"{GATEWAY}/tools/token_price/call"

VALID_402 = {
    "payment_id":  "fake-uuid-123",
    "amount_usdc": "0.001",
    "pay_to":      "GFAKEPAYTOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
}


@pytest.fixture
def fake_wallet():
    """Stub wallet that returns a synthetic successful payment without
    actually touching Stellar. The HTTP layer is what we're testing
    here, not the on-chain mechanics."""
    w = MagicMock()
    w.public_key = "GFAKEAGENTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    w.network    = "testnet"
    w.pay.return_value = {"success": True, "tx_hash": "fakehash" + "a" * 56}
    return w


# ── Happy path: 200 → tool result returned ──────────────────────────────────

class TestHappyPath:

    def test_200_returns_tool_result(self, fake_wallet):
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            # First call: 402
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(200, json={
                    "tool": "token_price",
                    "result": {"price_usd": 2070.13},
                    "payment": {"amount_usdc": "0.001", "tx_hash": "fakehash"},
                }),
            ])
            result = client.call_tool("token_price", {"symbol": "ETH"})

        assert result["tool"] == "token_price"
        assert result["result"]["price_usd"] == 2070.13


# ── Free tool ($0): SDK skips settlement, never calls wallet.pay ─────────────

class TestFreeTool:

    FREE_402 = {
        "payment_id":  "free-uuid-456",
        "amount_usdc": "0.000",
        "pay_to":      "GFAKEPAYTOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    }

    def test_free_tool_skips_payment(self, fake_wallet):
        """A $0 challenge must NOT trigger an on-chain payment. The wallet
        here would FAIL if paid (simulating an unfunded account), so the
        only way this passes is if the SDK skips settlement for $0 and
        retries with a free proof."""
        # If the SDK ever calls .pay here, the test fails loudly.
        fake_wallet.pay.side_effect = AssertionError("wallet.pay must not be called for a free tool")

        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            route = respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=self.FREE_402),
                httpx.Response(200, json={
                    "tool": "token_price",
                    "result": {"price_usd": 2070.13},
                    "payment": {"amount_usdc": "0.000", "tx_hash": ""},
                }),
            ])
            result = client.call_tool("token_price", {"symbol": "ETH"})

        assert result["result"]["price_usd"] == 2070.13
        assert fake_wallet.pay.call_count == 0
        # Retry carried a unique free proof derived from the payment_id.
        retry_req = route.calls[-1].request
        assert b"free:free-uuid-456" in retry_req.headers["X-Payment"].encode()
        # Recorded at $0 in the call log.
        assert client.call_log[-1]["amount_usdc"] == "0.000"


# ── 502 with refund_pending body → RefundPending raised ──────────────────────

class TestRefundPendingParse:

    def test_502_refund_pending_raises_typed_exception(self, fake_wallet):
        """Gateway PR #12 contract: tool fails post-verify → 502 with
        a body that carries payment_status='refund_pending',
        refund_eta_seconds=60, payment_id, and error_reason. The SDK
        should surface this as RefundPending, NOT a generic Exception,
        so callers can branch on it without parsing JSON.
        """
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(502, json={
                    "error":              "Tool execution failed",
                    "tool":               "token_price",
                    "payment_id":         "fake-uuid-123",
                    "payment_status":     "refund_pending",
                    "refund_eta_seconds": 60,
                    "error_reason":       "tool_exec_failed: upstream timeout",
                }),
            ])

            with pytest.raises(RefundPending) as exc_info:
                client.call_tool("token_price", {"symbol": "ETH"})

        e = exc_info.value
        assert e.payment_id          == "fake-uuid-123"
        assert e.refund_eta_seconds  == 60
        assert "tool_exec_failed"     in e.error_reason
        assert "upstream timeout"     in e.error_reason
        assert e.payment_status      == "refund_pending"
        # str(e) is the error_reason for readable logs
        assert "tool_exec_failed"     in str(e)

    def test_502_refund_disabled_raises_typed_exception_with_null_eta(self, fake_wallet):
        """Dark-launch path: REFUND_ENABLED=false on the gateway means
        the row is marked refund_pending in Supabase but no on-chain
        refund will fire. Body carries payment_status='refund_disabled'
        and refund_eta_seconds=null. SDK still raises RefundPending but
        with refund_eta_seconds=None — callers can use this to decide
        whether to wait (eta > 0) or escalate (eta is None).
        """
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(502, json={
                    "error":              "Tool execution failed",
                    "tool":               "token_price",
                    "payment_id":         "fake-uuid-123",
                    "payment_status":     "refund_disabled",
                    "refund_eta_seconds": None,
                    "error_reason":       "tool_exec_failed: oops",
                }),
            ])

            with pytest.raises(RefundPending) as exc_info:
                client.call_tool("token_price", {"symbol": "ETH"})

        e = exc_info.value
        assert e.payment_status     == "refund_disabled"
        assert e.refund_eta_seconds is None
        assert e.payment_id         == "fake-uuid-123"

    def test_502_unknown_body_shape_falls_back_to_generic_exception(self, fake_wallet):
        """Defensive: if the gateway returns 502 with a body that doesn't
        match the PR #12 contract — e.g. Railway edge served a plain
        500/502, or an unrelated gateway error — fall back to the
        generic Exception so the user still sees something useful.
        Backward-compatible with pre-#12 gateways."""
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(502, text="Internal Server Error"),
            ])

            with pytest.raises(Exception) as exc_info:
                client.call_tool("token_price", {"symbol": "ETH"})

        # Must NOT be a RefundPending — the body didn't say so
        assert not isinstance(exc_info.value, RefundPending)
        assert "Tool call failed after payment" in str(exc_info.value)

    def test_502_json_without_payment_status_falls_back(self, fake_wallet):
        """If the 502 body is valid JSON but doesn't have payment_status
        (e.g. an older gateway version, or some other failure mode like
        a malformed-tool-output reject), fall back to generic Exception
        rather than guessing at refund semantics."""
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(502, json={"error": "Something else broke"}),
            ])

            with pytest.raises(Exception) as exc_info:
                client.call_tool("token_price", {"symbol": "ETH"})

        assert not isinstance(exc_info.value, RefundPending)


# ── Payment failure path (existing pre-#12 behavior, regression guard) ──────

class TestPaymentFailedStillWorks:

    def test_wallet_pay_failure_raises_PaymentFailed(self, fake_wallet):
        """Pre-#12 sanity: if the on-chain payment itself fails (wallet
        empty, no trustline, etc.), the SDK should still raise
        PaymentFailed — not RefundPending. The gateway never got the
        payment, so there's nothing to refund."""
        fake_wallet.pay.return_value = {
            "success": False, "reason": "stellar:op_underfunded",
        }
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(402, json=VALID_402)
            )
            with pytest.raises(PaymentFailed) as exc_info:
                client.call_tool("token_price", {"symbol": "ETH"})

        assert "op_underfunded" in str(exc_info.value)
