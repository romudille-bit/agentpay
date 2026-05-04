"""
test_stellar.py — Tests for gateway/stellar.py with mocked Horizon.

verify_payment() is the most failure-prone module in the gateway — it has
two HTTP fallbacks (OZ facilitator → Horizon), six fail-modes per branch,
and crashes silently when Horizon misbehaves. These tests pin every
branch with respx-mocked HTTP responses.

Covered:
  - _verify_payment_horizon
      happy path (exact + overpayment), tx_not_found,
      tx_unsuccessful, no_matching_payment_op, wrong asset/issuer/
      from/to, amount_too_low, horizon_unreachable.
  - verify_payment
      facilitator 401 → Horizon fallback, facilitator rejects payload
      → Horizon fallback, no tx_hash provided → fail closed,
      facilitator unreachable → exception path.

Out of scope:
  - split_payment uses synchronous stellar_sdk Server.load_account /
    submit_transaction. Mocking that is a different exercise — covered
    in #15c (route-level integration tests can mock at the higher level).
  - get_usdc_balance — same story, sync stellar_sdk.

The default settings used by the test:
  STELLAR_NETWORK     = "testnet"
  USDC_ISSUER_TESTNET = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
  STELLAR_FACILITATOR_URL = "https://channels.openzeppelin.com/x402"

These come from gateway/config.py defaults; mock_settings doesn't
override them, so tests assert against horizon-testnet.stellar.org URLs.
"""

from decimal import Decimal

import httpx
import pytest
import respx

from gateway.stellar import _verify_payment_horizon, verify_payment


# ── Test data — canonical addresses + valid Horizon response shapes ─────────

AGENT_ADDR   = "GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENT"
GATEWAY_ADDR = "GTESTGATEWAYPUBLICKEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
USDC_ISSUER  = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
TX_HASH      = "abc123def456" * 5  # 60-char hex-ish string

HORIZON = "https://horizon-testnet.stellar.org"
FACILITATOR = "https://channels.openzeppelin.com/x402"


def _tx_response(successful=True):
    """Canonical /transactions/{hash} response."""
    return {"successful": successful, "hash": TX_HASH}


def _payment_op(
    *,
    asset_code="USDC",
    asset_issuer=USDC_ISSUER,
    from_addr=AGENT_ADDR,
    to_addr=GATEWAY_ADDR,
    amount="0.001",
):
    """Canonical USDC payment op as Horizon returns it."""
    return {
        "type":         "payment",
        "asset_code":   asset_code,
        "asset_issuer": asset_issuer,
        "from":         from_addr,
        "to":           to_addr,
        "amount":       amount,
    }


def _ops_response(*ops):
    """Canonical /transactions/{hash}/operations response."""
    return {"_embedded": {"records": list(ops)}}


# ── _verify_payment_horizon: direct Horizon path ─────────────────────────────

class TestVerifyPaymentHorizon:
    """The de facto production path. OZ facilitator returns 401 in
    production today, so every real payment goes through this."""

    @pytest.mark.asyncio
    async def test_happy_path_exact_amount(self, mock_settings):
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(_payment_op()))
            )
            result = await _verify_payment_horizon(
                tx_hash=TX_HASH,
                from_address=AGENT_ADDR,
                to_address=GATEWAY_ADDR,
                amount_usdc="0.001",
            )
        assert result == {"verified": True, "tx_hash": TX_HASH}

    @pytest.mark.asyncio
    async def test_overpayment_still_verifies(self, mock_settings):
        # Agent paid 0.005 but only 0.001 was required — should still verify.
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(
                    _payment_op(amount="0.005")
                ))
            )
            result = await _verify_payment_horizon(
                tx_hash=TX_HASH, from_address=AGENT_ADDR,
                to_address=GATEWAY_ADDR, amount_usdc="0.001",
            )
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_tx_not_found_returns_404(self, mock_settings):
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(404)
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False
        assert "not found" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_tx_unsuccessful(self, mock_settings):
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response(successful=False))
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False
        assert "successful" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_horizon_5xx_on_tx_endpoint(self, mock_settings):
        # Horizon returns 503 — should report the status code, not crash.
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(503)
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False
        assert "503" in result["reason"]

    @pytest.mark.asyncio
    async def test_operations_endpoint_non_200(self, mock_settings):
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(500)
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False
        assert "operations" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_no_payment_op_in_tx(self, mock_settings):
        # Tx exists but contains only manage_data — no payment op.
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(
                    {"type": "manage_data"}
                ))
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False
        assert "no matching" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_wrong_asset_code(self, mock_settings):
        # Payment was for a different asset (e.g. EURC, not USDC).
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(
                    _payment_op(asset_code="EURC")
                ))
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False

    @pytest.mark.asyncio
    async def test_wrong_asset_issuer(self, mock_settings):
        # Same code (USDC) but different issuer — could be a fake USDC.
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(
                    _payment_op(asset_issuer="GFAKEISSUER")
                ))
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False

    @pytest.mark.asyncio
    async def test_wrong_destination(self, mock_settings):
        # Payment sent to a different address than expected.
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(
                    _payment_op(to_addr="GDIFFERENTGATEWAY")
                ))
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False

    @pytest.mark.asyncio
    async def test_wrong_source(self, mock_settings):
        # Someone else paid, not the agent that's claiming the payment.
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(
                    _payment_op(from_addr="GIMPOSTOR")
                ))
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False

    @pytest.mark.asyncio
    async def test_amount_too_low(self, mock_settings):
        # Agent paid 0.0005 but 0.001 was required.
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(
                    _payment_op(amount="0.0005")
                ))
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False
        assert "0.0005" in result["reason"]

    @pytest.mark.asyncio
    async def test_horizon_unreachable_handles_gracefully(self, mock_settings):
        # Network failure — respx raises a transport error.
        with respx.mock:
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                side_effect=httpx.ConnectError("dns fail")
            )
            result = await _verify_payment_horizon(
                TX_HASH, AGENT_ADDR, GATEWAY_ADDR, "0.001",
            )
        assert result["verified"] is False


# ── verify_payment: facilitator → Horizon fallthrough ────────────────────────

class TestVerifyPayment:
    """The public entry point. In production OZ always 401s, so the
    Horizon fallthrough is the actual hot path. These tests pin both
    routes."""

    @pytest.mark.asyncio
    async def test_facilitator_401_falls_back_to_horizon(self, mock_settings):
        # The current production behaviour: OZ facilitator 401, Horizon takes over.
        with respx.mock:
            respx.post(f"{FACILITATOR}/verify").mock(
                return_value=httpx.Response(401, json={"error": "auth required"})
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(_payment_op()))
            )
            result = await verify_payment(
                from_address=AGENT_ADDR,
                to_address=GATEWAY_ADDR,
                amount_usdc="0.001",
                payment_id="some-payment-id",
                tx_hash=TX_HASH,
            )
        assert result == {"verified": True, "tx_hash": TX_HASH}

    @pytest.mark.asyncio
    async def test_facilitator_401_no_tx_hash_fails_closed(self, mock_settings):
        # OZ unreachable AND no tx_hash to fall back on — fail closed.
        with respx.mock:
            respx.post(f"{FACILITATOR}/verify").mock(
                return_value=httpx.Response(401)
            )
            result = await verify_payment(
                from_address=AGENT_ADDR,
                to_address=GATEWAY_ADDR,
                amount_usdc="0.001",
                payment_id="some-payment-id",
                # tx_hash deliberately omitted
            )
        assert result["verified"] is False
        assert "facilitator" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_facilitator_rejects_with_invalid_falls_back(self, mock_settings):
        # OZ accepts the request but says invalid — same fallback to Horizon
        # if we have a tx_hash.
        with respx.mock:
            respx.post(f"{FACILITATOR}/verify").mock(
                return_value=httpx.Response(200, json={
                    "isValid": False,
                    "invalidReason": "no auth"
                })
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(_payment_op()))
            )
            result = await verify_payment(
                from_address=AGENT_ADDR,
                to_address=GATEWAY_ADDR,
                amount_usdc="0.001",
                payment_id="some-payment-id",
                tx_hash=TX_HASH,
            )
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_facilitator_unreachable_falls_back_to_horizon(self, mock_settings):
        # Network failure on the facilitator post should fall back to Horizon
        # if a tx_hash is provided. Added by #17 — was previously a hard fail.
        with respx.mock:
            respx.post(f"{FACILITATOR}/verify").mock(
                side_effect=httpx.ConnectError("facilitator down")
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(_payment_op()))
            )
            result = await verify_payment(
                from_address=AGENT_ADDR,
                to_address=GATEWAY_ADDR,
                amount_usdc="0.001",
                payment_id="some-payment-id",
                tx_hash=TX_HASH,
            )
        assert result == {"verified": True, "tx_hash": TX_HASH}

    @pytest.mark.asyncio
    async def test_facilitator_unreachable_no_tx_hash_fails_closed(self, mock_settings):
        # Network failure + no tx_hash to fall back on → fail closed.
        with respx.mock:
            respx.post(f"{FACILITATOR}/verify").mock(
                side_effect=httpx.ConnectError("facilitator down")
            )
            result = await verify_payment(
                from_address=AGENT_ADDR,
                to_address=GATEWAY_ADDR,
                amount_usdc="0.001",
                payment_id="some-payment-id",
                # tx_hash deliberately omitted
            )
        assert result["verified"] is False
        assert "facilitator" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_facilitator_5xx_falls_back_to_horizon(self, mock_settings):
        # A 5xx from the facilitator should also trigger fallback, not just
        # 401. This is the headline #17 fix — previously the gateway would
        # return spurious payment-verification failures during facilitator
        # outages even when the on-chain settlement was successful.
        for code in (500, 502, 503, 504):
            with respx.mock:
                respx.post(f"{FACILITATOR}/verify").mock(
                    return_value=httpx.Response(code)
                )
                respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                    return_value=httpx.Response(200, json=_tx_response())
                )
                respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                    return_value=httpx.Response(200, json=_ops_response(_payment_op()))
                )
                result = await verify_payment(
                    from_address=AGENT_ADDR,
                    to_address=GATEWAY_ADDR,
                    amount_usdc="0.001",
                    payment_id="some-payment-id",
                    tx_hash=TX_HASH,
                )
            assert result["verified"] is True, f"5xx code {code} should fall back to Horizon"


# ── #18 — facilitator flag-gating (default disabled) ────────────────────────

class TestFacilitatorDisabled:
    """When STELLAR_FACILITATOR_ENABLED is False (the new default after #18),
    verify_payment skips the OZ POST entirely and goes straight to Horizon.
    Saves ~15s of wasted timeout per call in production where OZ has been
    returning 401 for months."""

    @pytest.mark.asyncio
    async def test_disabled_skips_oz_and_uses_horizon(self, mock_settings, monkeypatch):
        # Override the mock_settings default (which sets ENABLED=true for the
        # OZ-flow tests) — this class tests the disabled path explicitly.
        import gateway.stellar
        mock_settings.STELLAR_FACILITATOR_ENABLED = False
        monkeypatch.setattr(gateway.stellar, "settings", mock_settings)

        with respx.mock:
            # Note: NO mock for the facilitator POST — if the code tries to
            # call it, respx will raise an unmatched-request error and the
            # test will fail. That's the assertion: with ENABLED=False we
            # should never hit the OZ endpoint.
            respx.get(f"{HORIZON}/transactions/{TX_HASH}").mock(
                return_value=httpx.Response(200, json=_tx_response())
            )
            respx.get(f"{HORIZON}/transactions/{TX_HASH}/operations").mock(
                return_value=httpx.Response(200, json=_ops_response(_payment_op()))
            )
            result = await verify_payment(
                from_address=AGENT_ADDR,
                to_address=GATEWAY_ADDR,
                amount_usdc="0.001",
                payment_id="some-payment-id",
                tx_hash=TX_HASH,
            )
        assert result == {"verified": True, "tx_hash": TX_HASH}

    @pytest.mark.asyncio
    async def test_disabled_no_tx_hash_fails_closed(self, mock_settings, monkeypatch):
        # Disabled + no tx_hash → cannot verify → fail closed with a
        # descriptive reason. No respx mocks needed since we never make
        # any HTTP calls.
        import gateway.stellar
        mock_settings.STELLAR_FACILITATOR_ENABLED = False
        monkeypatch.setattr(gateway.stellar, "settings", mock_settings)

        result = await verify_payment(
            from_address=AGENT_ADDR,
            to_address=GATEWAY_ADDR,
            amount_usdc="0.001",
            payment_id="some-payment-id",
            # tx_hash deliberately omitted
        )
        assert result["verified"] is False
        assert "disabled" in result["reason"].lower()
