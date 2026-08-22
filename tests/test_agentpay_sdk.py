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
from agentpay._wallet import PaymentFailed, RefundPending, SettlementUncertain


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


# ── Funding-wall hint on PaymentFailed (Phase 1.1) ───────────────────────────

class TestFundingHint:
    """Underfunded payment failures name the agent's own fundable
    address(es) so the agent (or its human) knows exactly what to fund."""

    def test_underfunded_names_stellar_address(self, fake_wallet):
        fake_wallet.base_address = None
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
        msg = str(exc_info.value)
        assert fake_wallet.public_key in msg
        assert "fund" in msg.lower()
        assert "0x" not in msg  # no Base wallet → no Base hint

    def test_underfunded_names_base_address_when_available(self, fake_wallet):
        fake_wallet.base_address = "0x" + "b" * 40
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
        msg = str(exc_info.value)
        assert fake_wallet.base_address in msg

    def test_non_funding_failure_keeps_plain_reason(self, fake_wallet):
        fake_wallet.base_address = None
        fake_wallet.pay.return_value = {
            "success": False, "reason": "stellar:tx_bad_seq",
        }
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(402, json=VALID_402)
            )
            with pytest.raises(PaymentFailed) as exc_info:
                client.call_tool("token_price", {"symbol": "ETH"})
        assert "fund" not in str(exc_info.value).lower()


# ── quickstart() mints a Base/EVM key client-side (Phase 1.1) ────────────────

class TestQuickstartEvmMint:

    def test_quickstart_mints_base_key(self):
        from stellar_sdk import Keypair
        from agentpay.client import quickstart

        kp = Keypair.random()
        register_resp = {
            "session_token": "tok-123",
            "free_tools": ["token_price"],
            "wallet": {
                "network": "stellar",
                "public_key": kp.public_key,
                "secret_key": kp.secret,
                "funded": False,
            },
        }
        with respx.mock:
            respx.post(f"{GATEWAY}/v1/agent/register").mock(
                return_value=httpx.Response(200, json=register_resp)
            )
            s = quickstart(gateway_url=GATEWAY, quiet=True)

        # eth_account is installed in the dev env, so a Base key is minted
        # locally and the wallet can settle on the default paid chain.
        assert s.base_public_key and s.base_public_key.startswith("0x")
        assert len(s.base_public_key) == 42
        assert s.base_secret_key and s.base_secret_key.startswith("0x")
        assert s.wallet.base_address == s.base_public_key
        assert s.wallet_public_key == kp.public_key

    def test_quickstart_byo_base_key_not_overwritten(self):
        from stellar_sdk import Keypair
        from eth_account import Account
        from agentpay.client import quickstart

        kp = Keypair.random()
        own = Account.create()
        register_resp = {
            "session_token": "tok-123",
            "free_tools": [],
            "wallet": {
                "network": "stellar",
                "public_key": kp.public_key,
                "secret_key": kp.secret,
                "funded": False,
            },
        }
        with respx.mock:
            respx.post(f"{GATEWAY}/v1/agent/register").mock(
                return_value=httpx.Response(200, json=register_resp)
            )
            s = quickstart(
                gateway_url=GATEWAY, quiet=True,
                base_key="0x" + own.key.hex(),
            )
        assert s.base_public_key == own.address
        assert s.base_secret_key is None  # brought, not minted — never echoed


# ── Balance check: empty vs unreachable (Phase 2.3) ──────────────────────────

class TestBalanceErrorContract:

    def _wallet(self):
        from stellar_sdk import Keypair
        from agentpay._wallet import AgentWallet
        return AgentWallet(secret_key=Keypair.random().secret, network="testnet")

    def test_unfunded_account_is_zero(self, monkeypatch):
        from stellar_sdk.exceptions import NotFoundError
        w = self._wallet()
        def raise_not_found(_pk):
            raise NotFoundError.__new__(NotFoundError)
        monkeypatch.setattr(w.server, "load_account", raise_not_found)
        assert w.get_usdc_balance() == "0"

    def test_horizon_down_raises_not_zero(self, monkeypatch):
        w = self._wallet()
        def raise_conn(_pk):
            raise ConnectionError("horizon unreachable")
        monkeypatch.setattr(w.server, "load_account", raise_conn)
        with pytest.raises(RuntimeError, match="balance check failed"):
            w.get_usdc_balance()


# ── Base-disabled diagnostics (0.2.6 polish) ─────────────────────────────────

class TestBaseDisabledReason:

    def test_bad_base_key_records_reason(self):
        from stellar_sdk import Keypair
        from agentpay._wallet import AgentWallet
        w = AgentWallet(secret_key=Keypair.random().secret, network="testnet",
                        base_key="not-a-valid-key")
        assert w.base_address is None
        assert w.base_disabled_reason and "rejected" in w.base_disabled_reason

    def test_funding_hint_includes_disabled_reason(self, fake_wallet):
        fake_wallet.base_address = None
        fake_wallet.base_disabled_reason = (
            'eth_account not installed — run: pip install "agentpay-x402[base]" '
            "(if you have a venv, make sure it's activated)"
        )
        fake_wallet.pay.return_value = {
            "success": False, "reason": "stellar:Resource Missing",
        }
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(402, json=VALID_402)
            )
            with pytest.raises(PaymentFailed) as exc_info:
                client.call_tool("token_price", {"symbol": "ETH"})
        msg = str(exc_info.value)
        assert "Base settlement unavailable" in msg
        assert "agentpay-x402[base]" in msg


class TestPaymentRequiredHeaderDecode:
    """AGE-9: _decode_payment_required_header — x402 v2 header fallback for
    external 402s whose body carries no accepts."""

    def test_base64_header(self):
        import base64, json
        from agentpay._wallet import _decode_payment_required_header
        payload = {"accepts": [{"network": "eip155:8453", "amount": "10000"}]}
        raw = base64.b64encode(json.dumps(payload).encode()).decode()
        assert _decode_payment_required_header({"PAYMENT-REQUIRED": raw}) == payload

    def test_raw_json_and_casing(self):
        import json
        from agentpay._wallet import _decode_payment_required_header
        payload = {"accepts": []}
        assert _decode_payment_required_header(
            {"x-payment-required": json.dumps(payload)}) == payload

    def test_absent_or_garbage_is_none(self):
        from agentpay._wallet import _decode_payment_required_header
        assert _decode_payment_required_header({}) is None
        assert _decode_payment_required_header({"PAYMENT-REQUIRED": "%%%"}) is None


# ═════════════════════════════════════════════════════════════════════════════
# Gateway Code Review 2026-07 — SDK cluster regression tests (AGE-53..57)
# ═════════════════════════════════════════════════════════════════════════════

from decimal import Decimal

from agentpay._wallet import (
    BudgetExceeded,
    PrePaymentError,
    Session,
)


def _session_wallet():
    """Stub wallet for Session-level tests: Stellar-only, successful pays."""
    w = MagicMock()
    w.public_key = "GFAKEAGENTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    w.network = "testnet"
    w.base_address = None
    w.base_disabled_reason = None
    w.pay.return_value = {"success": True, "tx_hash": "fakehash" + "a" * 56}
    return w


TOKEN_PRICE_INFO = {"name": "token_price", "price_usdc": "0.001", "category": "data"}
TOOLS_LIST = {"tools": [
    {"name": "token_price", "price_usdc": "0.001", "category": "data", "active": True},
    {"name": "gas_tracker", "price_usdc": "0.001", "category": "data", "active": True},
]}


class TestBudgetCapBindsActualAmount:
    """AGE-53: the cap must bind the amount the 402 ACTUALLY demands,
    not the registry-advertised price."""

    def test_client_refuses_402_above_cap_before_paying(self, fake_wallet):
        fake_wallet.base_address = None
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        inflated = dict(VALID_402, amount_usdc="0.50")
        with respx.mock:
            respx.post(TOOL_URL).mock(return_value=httpx.Response(402, json=inflated))
            with pytest.raises(BudgetExceeded) as exc_info:
                client.call_tool("token_price", {"symbol": "ETH"}, max_spend="0.00105")
        assert "refusing to pay" in str(exc_info.value)
        assert fake_wallet.pay.call_count == 0        # nothing moved
        assert client.call_log == []                   # nothing recorded

    def test_session_binds_quote_not_budget(self):
        """Budget is $1.00 but the registry quote is $0.001 — a 402 demanding
        $0.50 must be refused even though it fits the session budget."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        inflated = dict(VALID_402, amount_usdc="0.50")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.post(TOOL_URL).mock(return_value=httpx.Response(402, json=inflated))
            with pytest.raises(BudgetExceeded):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 0
        assert s.spent_usd() == Decimal("0")

    def test_402_within_tolerance_is_paid(self):
        """Small overpay (<5% above quote) is tolerated — rounding/format
        drift must not brick every call."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        slightly_up = dict(VALID_402, amount_usdc="0.00104")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=slightly_up),
                httpx.Response(200, json={
                    "tool": "token_price", "result": {"price_usd": 1.0},
                    "payment": {"amount_usdc": "0.00104", "tx_hash": "t",
                                "network": "stellar-testnet"},
                }),
            ])
            result = s.call("token_price", {"symbol": "ETH"})
        assert result["result"]["price_usd"] == 1.0
        assert w.pay.call_count == 1
        assert s.spent_usd() == Decimal("0.00104")


class TestSpendRecordedOnBroadcast:
    """AGE-54: a broadcast payment counts against the budget even when the
    tool call then fails — spent() must never under-report."""

    def test_client_records_spend_when_retry_fails(self, fake_wallet):
        fake_wallet.base_address = None
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(500, text="upstream exploded"),
            ])
            with pytest.raises(Exception, match="Tool call failed after payment"):
                client.call_tool("token_price", {"symbol": "ETH"})
        assert len(client.call_log) == 1
        e = client.call_log[0]
        assert e["amount_usdc"] == "0.001"
        assert e["success"] is False
        assert e["state"] == "paid_no_result"
        assert e["tx_hash"].startswith("fakehash")

    def test_session_burns_budget_on_pay_then_fail(self):
        """The exact bug: an agent looping over a pay-then-fail tool used to
        spend real USDC every iteration while remaining() stayed full."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(500, text="boom"),
            ])
            with pytest.raises(Exception, match="after payment"):
                s.call("token_price", {"symbol": "ETH"})
        assert s.spent_usd() == Decimal("0.001")           # budget burned
        assert s.summary()["breakdown"][0]["success"] is False

    def test_refund_pending_spend_counts_until_refund_confirms(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(502, json={
                    "payment_id": "fake-uuid-123",
                    "payment_status": "refund_pending",
                    "refund_eta_seconds": 60,
                    "error_reason": "tool_exec_failed: x",
                }),
            ])
            with pytest.raises(RefundPending):
                s.call("token_price", {"symbol": "ETH"})
        assert s.spent_usd() == Decimal("0.001")
        assert s.summary()["breakdown"][0]["state"] == "refund_pending"


class TestNoFallbackAfterPayment:
    """AGE-55: fallback only on pre-payment failures — a post-payment
    failure must never trigger a second payment."""

    def test_post_payment_failure_does_not_pay_fallback(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        fallback_url = f"{GATEWAY}/tools/gas_tracker/call"
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(500, text="post-payment boom"),
            ])
            fb_route = respx.post(fallback_url).mock(
                return_value=httpx.Response(402, json=VALID_402))
            with pytest.raises(Exception, match="after payment"):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 1          # exactly one payment
        assert not fb_route.called            # fallback never touched

    def test_pre_payment_failure_still_falls_back(self):
        """A 503 on the un-paid probe is pre-payment — fallback stays."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00", fallback="auto")
        fallback_url = f"{GATEWAY}/tools/gas_tracker/call"
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(503, text="down"))
            respx.post(fallback_url).mock(side_effect=[
                httpx.Response(402, json=dict(VALID_402, payment_id="fb-1")),
                httpx.Response(200, json={
                    "tool": "gas_tracker", "result": {"gwei": 12},
                    "payment": {"amount_usdc": "0.001", "tx_hash": "t"},
                }),
            ])
            result = s.call("token_price", {"symbol": "ETH"})
        assert result["result"]["gwei"] == 12
        assert w.pay.call_count == 1                       # only the fallback paid
        assert s.summary()["breakdown"][0]["fallback_for"] == "token_price"
        assert s.spent_usd() == Decimal("0.001")


class TestSignedAuthNotTreatedAsUnspent:
    """AGE-56: once the signed EIP-3009 auth is transmitted, a non-200 must
    NOT be treated as 'no payment settled' — no Stellar re-pay, spend
    recorded as uncertain."""

    def _base_402(self):
        return dict(VALID_402, payment_options={"base": {
            "amount_atomic": 1000, "amount_usdc": "0.001",
            "pay_to": "0x" + "c" * 40, "network": "eip155:8453",
        }})

    def test_no_stellar_fallback_after_auth_transmitted(self, fake_wallet):
        fake_wallet.base_address = "0x" + "b" * 40
        fake_wallet.build_base_payment_signature.return_value = "sig-b64"
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=self._base_402()),
                httpx.Response(500, text="server pretends nothing settled"),
            ])
            with pytest.raises(SettlementUncertain):
                client.call_tool("token_price", {"symbol": "ETH"})
        assert fake_wallet.pay.call_count == 0             # NO Stellar re-pay
        e = client.call_log[0]
        assert e["state"] == "uncertain_settlement"        # counted, not "free"
        assert e["amount_usdc"] == "0.001"

    def test_signing_failure_is_prepayment_and_falls_back_to_stellar(self, fake_wallet):
        """Failures BEFORE transmission (signing) are pre-payment: the old
        Stellar fallback behaviour is preserved there."""
        fake_wallet.base_address = "0x" + "b" * 40
        fake_wallet.build_base_payment_signature.side_effect = RuntimeError("no signer")
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=self._base_402()),
                httpx.Response(200, json={
                    "tool": "token_price", "result": {"price_usd": 1.0},
                    "payment": {"amount_usdc": "0.001", "tx_hash": "t"},
                }),
            ])
            result = client.call_tool("token_price", {"symbol": "ETH"})
        assert result["result"]["price_usd"] == 1.0
        assert fake_wallet.pay.call_count == 1             # settled on Stellar

    def test_base_option_amount_also_bound_by_cap(self, fake_wallet):
        """The Base block can carry its own amount — it must be capped too,
        BEFORE signing."""
        fake_wallet.base_address = "0x" + "b" * 40
        challenge = dict(VALID_402, payment_options={"base": {
            "amount_atomic": 500_000,   # $0.50 despite body saying $0.001
            "amount_usdc": "0.50",
            "pay_to": "0x" + "c" * 40, "network": "eip155:8453",
        }})
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(return_value=httpx.Response(402, json=challenge))
            with pytest.raises(BudgetExceeded, match="refusing to sign"):
                client.call_tool("token_price", {}, max_spend="0.00105")
        assert fake_wallet.build_base_payment_signature.call_count == 0
        assert fake_wallet.pay.call_count == 0


class TestUrlCallsRespectPolicies:
    """AGE-57: allowed_tools / max_per_tool / rate_limit apply to external
    x402 URLs exactly as they do to registry tools."""

    EVIL = "https://evil.example/x402/steal"

    def test_allowlist_blocks_external_url(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00",
                    allowed_tools=["token_price"])
        with respx.mock:   # no routes: ANY http request would error loudly
            with pytest.raises(BudgetExceeded, match="allowlist"):
                s.call(self.EVIL, {"q": "x"})
        assert w.pay.call_count == 0

    def test_per_tool_cap_applies_to_url(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00",
                    max_per_tool={self.EVIL: 0.001})
        s._call_log.append({"tool": self.EVIL, "amount_usdc": "0.001"})
        with respx.mock:
            with pytest.raises(BudgetExceeded, match="Per-tool cap"):
                s.call(self.EVIL, {"q": "x"})

    def test_rate_limit_applies_to_url(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00", rate_limit=0)
        with respx.mock:
            with pytest.raises(BudgetExceeded, match="Rate limit"):
                s.call(self.EVIL, {"q": "x"})


class TestExternalUrlSpendRecording:
    """AGE-54/56 for the external-URL path in Session._call_x402_url."""

    URL = "https://ext.example/tool"

    def _stellar_accepts(self):
        return {"accepts": [{
            "network": "stellar:pubnet", "amount": "1000",
            "payTo": "GEXTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "scheme": "exact",
        }]}

    def _base_accepts(self):
        return {"accepts": [{
            "network": "eip155:8453", "amount": "1000",
            "payTo": "0x" + "c" * 40, "scheme": "exact",
        }]}

    def test_external_stellar_spend_recorded_on_failed_retry(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        with respx.mock:
            respx.post(self.URL).mock(side_effect=[
                httpx.Response(402, json=self._stellar_accepts()),
                httpx.Response(500, text="post-payment boom"),
            ])
            with pytest.raises(Exception, match="spend recorded"):
                s.call(self.URL, {"q": "x"})
        assert w.pay.call_count == 1
        assert s.spent_usd() == Decimal("0.001000")
        assert s.summary()["breakdown"][0]["state"] == "paid_no_result"

    def test_external_base_rejection_still_counts_spend(self):
        w = _session_wallet()
        w.base_address = "0x" + "b" * 40
        w.build_base_payment_signature.return_value = "sig-b64"
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        with respx.mock:
            respx.post(self.URL).mock(side_effect=[
                httpx.Response(402, json=self._base_accepts()),
                httpx.Response(500, text="rejected, or was it"),
            ])
            with pytest.raises(Exception, match="settlement uncertain"):
                s.call(self.URL, {"q": "x"})
        assert w.pay.call_count == 0                       # never re-paid on Stellar
        assert s.spent_usd() == Decimal("0.001000")
        assert s.summary()["breakdown"][0]["state"] == "uncertain_settlement"


class TestFallbackRespectsPolicies:
    """AGE-57 follow-up: a FALLBACK tool must satisfy the same session
    policies — the SDK picking it doesn't exempt it from the allowlist
    or a per-tool cap."""

    def test_fallback_outside_allowlist_is_not_paid(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00",
                    allowed_tools=["token_price"], fallback="auto")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(503, text="down"))
            fb_route = respx.post(f"{GATEWAY}/tools/gas_tracker/call").mock(
                return_value=httpx.Response(402, json=VALID_402))
            with pytest.raises(PrePaymentError):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 0
        assert not fb_route.called

    def test_fallback_at_per_tool_cap_is_not_paid(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00",
                    max_per_tool={"gas_tracker": 0.001}, fallback="auto")
        s._call_log.append({"tool": "gas_tracker", "amount_usdc": "0.001"})
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(503, text="down"))
            fb_route = respx.post(f"{GATEWAY}/tools/gas_tracker/call").mock(
                return_value=httpx.Response(402, json=VALID_402))
            with pytest.raises(PrePaymentError):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 0
        assert not fb_route.called


class TestBaseLegRecordsSignedAmount:
    """Review follow-up: the Base leg must record the amount the auth was
    actually SIGNED for (payment_options.base), not the 402 body's
    amount_usdc — the signed amount is the one that can settle."""

    def test_entry_amount_is_the_signed_base_amount(self, fake_wallet):
        fake_wallet.base_address = "0x" + "b" * 40
        fake_wallet.build_base_payment_signature.return_value = "sig-b64"
        challenge = dict(VALID_402, amount_usdc="0.001", payment_options={"base": {
            "amount_atomic": 1050,          # $0.00105 signed — differs from body
            "amount_usdc": "0.00105",
            "pay_to": "0x" + "c" * 40, "network": "eip155:8453",
        }})
        client = AgentPayClient(wallet=fake_wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=challenge),
                httpx.Response(500, text="rejected"),
            ])
            with pytest.raises(SettlementUncertain):
                client.call_tool("token_price", {}, max_spend="0.00105")
        e = client.call_log[0]
        assert e["amount_usdc"] == "0.00105"   # signed amount, not body's 0.001
        assert e["state"] == "uncertain_settlement"


class TestBudgetReservationLock:
    """AGE-66: the budget check→reserve→spend sequence is lock-guarded so two
    concurrent call()s can't both pass would_exceed and double-pay."""

    def test_failed_fallback_rereserve_does_not_double_release(self):
        """Regression (adversarial review 2026-07): on the PrePaymentError
        fallback path, the original hold is released and then re-reserved at the
        fallback price. If the re-reserve loses a concurrent race and raises,
        the `finally` must NOT release the (already-released) original hold a
        second time — a double-release drives _reserved negative and lets the
        budget be overspent by a leg price. After the raise, _reserved must be
        back to exactly 0, never negative."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00", fallback="auto")

        real_reserve = s._reserve
        calls = {"n": 0}

        def flaky_reserve(amount):
            calls["n"] += 1
            # First reserve (original tool) succeeds; the fallback re-reserve
            # simulates a concurrent loser and fails.
            if calls["n"] == 1:
                return real_reserve(amount)
            return False

        s._reserve = flaky_reserve

        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            # 503 on the un-paid probe → PrePaymentError → fallback re-point.
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(503, text="down"))
            with pytest.raises(BudgetExceeded):
                s.call("token_price", {"symbol": "ETH"})

        # The hold accounting is balanced: not leaked, not double-released.
        assert s._reserved == Decimal("0")
        assert s._reserved >= Decimal("0")
        # And the budget is fully available again — no phantom overspend room
        # and no phantom debt.
        assert s.remaining_usd() == Decimal("1.00")

    def test_reserve_counts_against_remaining(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="0.010")
        assert s.remaining_usd() == Decimal("0.010")
        assert s._reserve("0.007") is True
        # The hold shows up in remaining/would_exceed immediately.
        assert s.remaining_usd() == Decimal("0.003")
        assert s.would_exceed("0.005") is True        # 0.007 held + 0.005 > 0.010
        assert s.would_exceed("0.003") is False
        # A second reservation that wouldn't fit is refused (no partial hold).
        assert s._reserve("0.005") is False
        assert s.remaining_usd() == Decimal("0.003")
        s._release("0.007")
        assert s.remaining_usd() == Decimal("0.010")

    def test_concurrent_calls_cannot_exceed_budget(self, monkeypatch):
        """Two threads race on a budget that fits only ONE $0.001 call. With
        the reservation lock, exactly one pays and total spend never exceeds
        the cap; the loser raises BudgetExceeded."""
        import threading
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="0.001")

        start = threading.Barrier(2)
        results = []

        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            # A slow-ish 402→200 so both threads overlap inside call().
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(200, json={"tool": "token_price",
                                          "result": {"price_usd": 1.0},
                                          "payment": {"amount_usdc": "0.001",
                                                      "tx_hash": "t",
                                                      "network": "stellar-testnet"}}),
                httpx.Response(402, json=VALID_402),
                httpx.Response(200, json={"tool": "token_price",
                                          "result": {"price_usd": 1.0},
                                          "payment": {"amount_usdc": "0.001",
                                                      "tx_hash": "t2",
                                                      "network": "stellar-testnet"}}),
            ])

            def worker():
                start.wait()
                try:
                    s.call("token_price", {"symbol": "ETH"})
                    results.append("ok")
                except BudgetExceeded:
                    results.append("blocked")
                except Exception as e:
                    results.append(f"err:{e}")

            ts = [threading.Thread(target=worker) for _ in range(2)]
            for t in ts: t.start()
            for t in ts: t.join()

        # Total spend never exceeds the cap, regardless of scheduling.
        assert s.spent_usd() <= Decimal("0.001")
        # At most one paid; the other was blocked (never a double-pay).
        assert results.count("ok") <= 1
        assert w.pay.call_count <= 1


class TestSubmitTimeoutPoll:
    """AGE-68: a Stellar submit that times out but actually landed must be
    reported as success (with the precomputed hash), not a failure the caller
    would retry into a double-pay."""

    def _wallet(self):
        from stellar_sdk import Keypair
        from agentpay._wallet import AgentWallet
        return AgentWallet(secret_key=Keypair.random().secret, network="testnet")

    def test_timeout_but_confirmed_returns_success(self, monkeypatch):
        w = self._wallet()
        monkeypatch.setattr(w.server, "load_account", lambda pk: MagicMock())

        class _Tx:
            def sign(self, kp): pass
            def hash_hex(self): return "abc123hash"
        monkeypatch.setattr(
            "agentpay._wallet.TransactionBuilder",
            lambda **k: _Builder(_Tx()))

        def _boom(tx):
            raise Exception("read timed out")
        monkeypatch.setattr(w.server, "submit_transaction", _boom)
        # Poll says it DID land.
        monkeypatch.setattr(w, "_await_tx_on_chain", lambda h, **k: True)

        out = w.pay("GDEST", "0.001", memo="m")
        assert out["success"] is True
        assert out["tx_hash"] == "abc123hash"

    def test_timeout_not_found_stays_failure(self, monkeypatch):
        w = self._wallet()
        monkeypatch.setattr(w.server, "load_account", lambda pk: MagicMock())

        class _Tx:
            def sign(self, kp): pass
            def hash_hex(self): return "abc123hash"
        monkeypatch.setattr(
            "agentpay._wallet.TransactionBuilder",
            lambda **k: _Builder(_Tx()))
        monkeypatch.setattr(w.server, "submit_transaction",
                            lambda tx: (_ for _ in ()).throw(Exception("read timed out")))
        monkeypatch.setattr(w, "_await_tx_on_chain", lambda h, **k: False)

        out = w.pay("GDEST", "0.001")
        assert out["success"] is False

    def test_clean_rejection_does_not_poll(self, monkeypatch):
        """A protocol rejection (result_codes) is definitive — no poll, failure."""
        w = self._wallet()
        monkeypatch.setattr(w.server, "load_account", lambda pk: MagicMock())

        class _Tx:
            def sign(self, kp): pass
            def hash_hex(self): return "abc123hash"
        monkeypatch.setattr(
            "agentpay._wallet.TransactionBuilder",
            lambda **k: _Builder(_Tx()))

        class _Rejected(Exception):
            extras = {"result_codes": {"operations": ["op_underfunded"]}}
        monkeypatch.setattr(w.server, "submit_transaction",
                            lambda tx: (_ for _ in ()).throw(_Rejected()))
        polled = {"n": 0}
        monkeypatch.setattr(w, "_await_tx_on_chain",
                            lambda h, **k: polled.__setitem__("n", polled["n"] + 1) or True)

        out = w.pay("GDEST", "0.001")
        assert out["success"] is False
        assert polled["n"] == 0            # never polled a clean rejection


class _Builder:
    """Minimal TransactionBuilder stand-in whose fluent methods return self."""
    def __init__(self, tx):
        self._tx = tx
    def add_text_memo(self, *a, **k): return self
    def append_payment_op(self, *a, **k): return self
    def set_timeout(self, *a, **k): return self
    def build(self): return self._tx


class TestPerToolCapWouldExceed:
    """AGE-74 #6: the per-tool cap blocks the call that WOULD CROSS the cap,
    not just the next call after it's already exceeded."""

    def test_call_that_crosses_cap_is_blocked(self):
        w = _session_wallet()
        # Cap the tool at $0.0015; one $0.001 call fits, a second would cross.
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00",
                    max_per_tool={"token_price": 0.0015})
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=VALID_402),
                httpx.Response(200, json={"tool": "token_price",
                                          "result": {"p": 1},
                                          "payment": {"amount_usdc": "0.001",
                                                      "tx_hash": "t",
                                                      "network": "stellar-testnet"}}),
            ])
            s.call("token_price", {"symbol": "ETH"})       # 0.001 spent, fits
        assert s.spent_usd() == Decimal("0.001")
        # Second call would bring spend to 0.002 > 0.0015 cap → blocked BEFORE paying.
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            with pytest.raises(BudgetExceeded, match="Per-tool cap"):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 1                        # never paid the 2nd


class TestBudgetPromptCtrlC:
    """AGE-74 #3 / AGE-120: Ctrl-C or EOF at the attended budget prompt must
    NOT silently authorize the default cap — both re-raise."""

    def test_keyboardinterrupt_reraises(self, monkeypatch):
        import sys as _sys
        import builtins
        from agentpay.budget_policy import budget_policy
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
        def _boom(*a, **k):
            raise KeyboardInterrupt()
        monkeypatch.setattr(builtins, "input", _boom)
        with pytest.raises(KeyboardInterrupt):
            budget_policy(interactive=True, usdc_balance=1.00)

    def test_eof_reraises(self, monkeypatch):
        import sys as _sys
        import builtins
        from agentpay.budget_policy import budget_policy
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
        def _eof(*a, **k):
            raise EOFError()
        monkeypatch.setattr(builtins, "input", _eof)
        with pytest.raises(EOFError):
            budget_policy(interactive=True, usdc_balance=1.00)


class TestKeyParseConstantMessage:
    """AGE-74 #4: a bad key raises a constant message, never echoing the key."""

    def test_bad_stellar_secret_constant_message(self):
        from agentpay._wallet import AgentWallet
        secret = "SBADKEYSHOULDNOTLEAK1234567890ABCDEF"
        with pytest.raises(ValueError) as ei:
            AgentWallet(secret_key=secret, network="testnet")
        assert secret not in str(ei.value)
        assert "invalid Stellar secret key" in str(ei.value)


# ── F1 (2026-07-20): the client-side cap must EXCLUDE this call's own hold ────

class TestCapExcludesOwnHold:
    """Follow-up review F1: _reserve() placed the hold before the cap was
    computed, and the cap used remaining_usd() which subtracts that very
    hold — so cap = min(remaining_before − price, 1.05·price). Exact-fit
    budgets failed deterministically and every session silently stranded
    its last call (regression shipped in 0.3.0, fixed in 0.3.1)."""

    def _mock_paid_call(self, url, pid, price="0.001"):
        return [
            httpx.Response(402, json=dict(VALID_402, payment_id=pid,
                                          amount_usdc=price)),
            httpx.Response(200, json={
                "tool": "token_price", "result": {"ok": True},
                "payment": {"amount_usdc": price, "tx_hash": "t",
                            "network": "stellar-testnet"},
            }),
        ]

    def test_exact_fit_budget_succeeds(self):
        """max_spend == price: the 0.3.0 cap was 0 → BudgetExceeded('402
        demands 0.001 … exceeds cap 0'). Must succeed."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="0.001")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.post(TOOL_URL).mock(
                side_effect=self._mock_paid_call(TOOL_URL, "exact-fit-1"))
            result = s.call("token_price", {"symbol": "ETH"})
        assert result["result"]["ok"] is True
        assert w.pay.call_count == 1
        assert s.spent_usd() == Decimal("0.001")
        assert s.remaining_usd() == Decimal("0")

    def test_last_call_exhausts_budget_succeeds(self):
        """A $0.003 session making three $0.001 calls must land all three.
        Under 0.3.0 the third call (remaining < 2× price) was falsely
        rejected — utilization silently capped at max_spend − price."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="0.003")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            calls = (self._mock_paid_call(TOOL_URL, "seq-1")
                     + self._mock_paid_call(TOOL_URL, "seq-2")
                     + self._mock_paid_call(TOOL_URL, "seq-3"))
            respx.post(TOOL_URL).mock(side_effect=calls)
            for _ in range(3):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 3
        assert s.spent_usd() == Decimal("0.003")
        assert s.remaining_usd() == Decimal("0")

    def test_concurrent_exact_fit_loser_fails_closed(self):
        """The fix must NOT weaken AGE-66: with another call's hold in
        flight consuming the whole budget, a second exact-fit call still
        fails closed — no payment, nothing recorded."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="0.001")
        # Simulate a concurrent in-flight call holding the entire budget.
        assert s._reserve("0.001") is True
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json={"tools": [
                    {"name": "token_price", "price_usdc": "0.001",
                     "category": "data", "active": True}]}))
            with pytest.raises(BudgetExceeded):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 0
        assert s.spent_usd() == Decimal("0")
        s._release("0.001")

    def test_tight_budget_fallback_succeeds(self):
        """F1 sibling: the fallback fit check ran while the original hold
        was still reserved, so exact-fit fallbacks were falsely rejected."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="0.001", fallback="auto")
        fallback_url = f"{GATEWAY}/tools/gas_tracker/call"
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(503, text="down"))   # pre-payment
            respx.post(fallback_url).mock(side_effect=[
                httpx.Response(402, json=dict(VALID_402, payment_id="fb-f1")),
                httpx.Response(200, json={
                    "tool": "gas_tracker", "result": {"gwei": 9},
                    "payment": {"amount_usdc": "0.001", "tx_hash": "t"},
                }),
            ])
            result = s.call("token_price", {"symbol": "ETH"})
        assert result["result"]["gwei"] == 9
        assert s.spent_usd() == Decimal("0.001")
        assert s.summary()["breakdown"][-1]["fallback_for"] == "token_price"


# ── F2 (2026-07-20): spend booked and hold dropped in ONE locked section ──────

class TestAbsorbAndReleaseAtomic:
    """Follow-up review F2: release-then-absorb was two lock acquisitions;
    in the gap _reserved was decremented but _spent not yet incremented, so
    a concurrent _reserve could over-commit by a leg price."""

    def test_absorb_and_release_books_and_drops_together(self):
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="0.10")
        assert s._reserve("0.001") is True
        fake_client = MagicMock()
        fake_client.call_log = [{
            "tool": "token_price", "amount_usdc": "0.001",
            "tx_hash": "t", "network": "stellar-testnet", "success": True,
        }]
        s._absorb_and_release(fake_client, requested="token_price",
                              target="token_price", held="0.001")
        assert s.spent_usd() == Decimal("0.001")
        assert s._reserved == Decimal("0")
        # Budget headroom is exact — no double-count, no leak.
        assert s.remaining_usd() == Decimal("0.099")

    def test_no_duplicate_receipt_rows_when_re_reserve_fails(self, monkeypatch):
        """Low from the follow-up review: if the fallback re-reserve raised,
        the finally re-absorbed the failed $0 leg → duplicate receipt rows."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00", fallback="auto")
        fallback_url = f"{GATEWAY}/tools/gas_tracker/call"
        real_reserve = s._reserve
        calls = {"n": 0}

        def flaky_reserve(amount):
            calls["n"] += 1
            if calls["n"] == 2:          # the fallback re-reserve
                return False
            return real_reserve(amount)

        monkeypatch.setattr(s, "_reserve", flaky_reserve)
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            respx.post(TOOL_URL).mock(
                return_value=httpx.Response(503, text="down"))   # pre-payment
            respx.post(fallback_url).mock(
                return_value=httpx.Response(402, json=VALID_402))
            with pytest.raises(BudgetExceeded, match="no longer fits"):
                s.call("token_price", {"symbol": "ETH"})
        rows = s.summary()["breakdown"]
        assert len(rows) == len({(r["tool"], r["amount_usdc"], r["tx_hash"],
                                  r.get("state", "")) for r in rows}), (
            f"duplicate receipt rows: {rows}")
        assert s.spent_usd() == Decimal("0")
        assert s._reserved == Decimal("0")


# ── F7 (2026-07-20): __version__ must match pyproject — pre-publish gate ──────

class TestVersionMatchesPyproject:
    """The 0.3.0 wheel shipped self-reporting 0.2.7 because only pyproject
    was bumped. This test is the release gate: bump BOTH or it fails."""

    def test_version_matches_pyproject(self):
        import pathlib
        import re

        import agentpay

        pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.M)
        assert m, "could not find version in pyproject.toml"
        assert agentpay.__version__ == m.group(1), (
            f"agentpay.__version__ ({agentpay.__version__}) != pyproject "
            f"({m.group(1)}) — bump both before publishing (F7)")


class TestFallbackOptIn:
    """AGE-118: tool substitution is opt-in; a typo is not a budget problem.

    - Unknown tool → typed ToolNotFound, NEVER substituted (even with
      fallback="auto" — the old code silently billed the cheapest
      category="data" tool for a typo).
    - Budget-breach rerouting only happens with Session(fallback="auto");
      the default ("off") raises BudgetExceeded with no substitution.
    - The kwarg is validated at construction.
    """

    def test_unknown_tool_raises_toolnotfound_default(self):
        from agentpay import ToolNotFound          # public export
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_pricee").mock(
                return_value=httpx.Response(404, json={"error": "not found"}))
            with pytest.raises(ToolNotFound, match="token_pricee"):
                s.call("token_pricee", {"symbol": "ETH"})
        assert w.pay.call_count == 0
        assert s.spent_usd() == Decimal("0")
        assert not isinstance(ToolNotFound("x"), BudgetExceeded)

    def test_unknown_tool_not_substituted_even_with_auto(self):
        """fallback="auto" restores budget/pre-payment rerouting, but a typo'd
        name still raises — there is no category to substitute within."""
        from agentpay._wallet import ToolNotFound
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00", fallback="auto")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_pricee").mock(
                return_value=httpx.Response(404, json={"error": "not found"}))
            tools_route = respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            with pytest.raises(ToolNotFound):
                s.call("token_pricee", {"symbol": "ETH"})
        assert w.pay.call_count == 0
        assert not tools_route.called      # no fallback lookup for a typo
        assert s.spent_usd() == Decimal("0")

    def test_budget_breach_default_raises_no_substitution(self):
        """Default fallback="off": over-budget call raises BudgetExceeded and
        the catalog is never consulted for a cheaper substitute."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="0.0005")   # < $0.001
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            tools_route = respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            with pytest.raises(BudgetExceeded):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 0
        assert not tools_route.called
        assert s.spent_usd() == Decimal("0")

    def test_pre_payment_failure_default_does_not_reroute(self):
        """Default fallback="off": a 503 before payment surfaces instead of
        silently billing a sibling tool (AGE-55 path, now gated)."""
        w = _session_wallet()
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/token_price").mock(
                return_value=httpx.Response(200, json=TOKEN_PRICE_INFO))
            respx.post(TOOL_URL).mock(return_value=httpx.Response(503, text="down"))
            tools_route = respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json=TOOLS_LIST))
            with pytest.raises(PrePaymentError):
                s.call("token_price", {"symbol": "ETH"})
        assert w.pay.call_count == 0
        assert not tools_route.called
        assert s.spent_usd() == Decimal("0")

    def test_fallback_kwarg_validated(self):
        w = _session_wallet()
        with pytest.raises(ValueError, match="fallback"):
            Session(w, gateway_url=GATEWAY, max_spend="1.00", fallback="on")
