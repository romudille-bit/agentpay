"""
test_x402_envelope_interop.py — regression for two external-x402 interop bugs
found on 2026-09-20 paying agents-trust.com, both in the AGE-90 series.

Bug 1 — the envelope hid `scheme` and `network`.
    A compliant v2 seller matches the payment payload back to one of its own
    advertised accepts[] entries on scheme+network, and a miss is answered
    with a fresh 402, never a silent accept. We carried both fields only
    nested inside `accepted`, so a seller looking at the top level saw
    neither. They belong at the top level, where the spec puts them.

Bug 2 — the paid retry ignored which method actually worked.
    A GET-only seller answers the POST probe with 405; the SDK re-probes with
    GET and gets the 402. It then chose the retry method from the 402's
    `bazaar` extension alone, defaulting to POST when the seller sends no
    such extension — so the paid retry went back out as a POST, drew a second
    405, and the authorization was already on the wire. Funds at risk, no
    data, and no retry permitted after transmission.

Bug 3 — the envelope dropped the seller's own extensions.
    Some sellers mint a signed, expiring payment-attempt token in the 402 and
    require it handed back with the payment to link the two. agents-trust.com
    rejects a payment without it (`payment_attempt_link_invalid`) and
    documents none of it. Echoing the seller's extensions back verbatim is
    the only general rule that works: it is their own data, opaque to us.

Together these made us unpayable by a standards-compliant GET-served seller.
"""

import base64
import json

import httpx
import pytest
import respx

from agentpay import Session
from tests.test_agentpay_sdk import GATEWAY, _session_wallet


def _wallet():
    from stellar_sdk import Keypair

    from agentpay._wallet import AgentWallet
    return AgentWallet(
        secret_key=Keypair.random().secret, network="testnet",
        base_key="0x" + "11" * 32,   # valid throwaway EVM key
    )


def _accept(**over):
    a = {
        "scheme": "exact",
        "network": "eip155:8453",
        "amount": "10000",
        "payTo": "0x" + "22" * 20,
        "maxTimeoutSeconds": 300,
    }
    a.update(over)
    return a


class TestEnvelopeCarriesSchemeAndNetwork:
    """Bug 1. The seller matches on top-level scheme+network."""

    def test_scheme_and_network_are_top_level(self):
        pytest.importorskip("x402")
        w = _wallet()
        accept = _accept(asset=w.BASE_USDC)
        decoded = json.loads(base64.b64decode(
            w.build_base_payment_signature(accept, "https://svc.example/tool")))
        assert decoded["scheme"] == "exact"
        assert decoded["network"] == "eip155:8453"

    def test_top_level_echoes_the_sellers_own_spelling(self):
        """Same reasoning as the `accepted` echo: a normalized copy can miss a
        strict comparison, so 'base' must stay 'base' at the top level too."""
        pytest.importorskip("x402")
        w = _wallet()
        accept = _accept(network="base", asset=w.BASE_USDC)
        decoded = json.loads(base64.b64decode(
            w.build_base_payment_signature(accept, "https://svc.example/tool")))
        assert decoded["network"] == "base"
        assert decoded["accepted"]["network"] == "base"
        # Normalization still feeds the SIGNER, not the declaration.
        assert decoded["payload"]["authorization"]["value"] == "10000"


class TestEnvelopeEchoesSellerExtensions:
    """Bug 3. Hand back whatever the seller handed us."""

    EXT = {"at-payment-attempt": {
        "info": {"paymentAttemptId": "01a0-fake", "mac": "abc", "keyId": "1"},
        "schema": {"type": "object"},
    }}

    def test_extensions_echoed_verbatim(self):
        pytest.importorskip("x402")
        w = _wallet()
        decoded = json.loads(base64.b64decode(w.build_base_payment_signature(
            _accept(asset=w.BASE_USDC), "https://svc.example/tool", self.EXT)))
        assert decoded["extensions"] == self.EXT

    def test_no_extensions_key_when_seller_sent_none(self):
        """Never invent a field the seller did not send — a strict matcher can
        reject an unexpected key as readily as a missing one."""
        pytest.importorskip("x402")
        w = _wallet()
        for empty in (None, {}):
            decoded = json.loads(base64.b64decode(w.build_base_payment_signature(
                _accept(asset=w.BASE_USDC), "https://svc.example/tool", empty)))
            assert "extensions" not in decoded


class TestGetOnlySellerIsPaidWithGet:
    """Bug 2. The retry follows the method that produced the 402."""

    URL = "https://ext.example/tool"

    def _base_402(self):
        return {"accepts": [_accept(payTo="0x" + "c" * 40)]}

    def test_paid_retry_uses_get_not_post(self):
        w = _session_wallet()
        w.base_address = "0x" + "b" * 40
        w.build_base_payment_signature.return_value = "sig-b64"
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        with respx.mock:
            post = respx.post(self.URL).mock(return_value=httpx.Response(405))
            get = respx.get(self.URL).mock(side_effect=[
                httpx.Response(402, json=self._base_402()),
                httpx.Response(200, json={"ok": True}),
            ])
            out = s.call(self.URL, {})
        assert out["ok"] is True
        assert post.call_count == 1          # probed once, 405, never again
        assert get.call_count == 2           # 402 probe, then the PAID retry
        paid = get.calls[1].request
        assert paid.headers.get("PAYMENT-SIGNATURE") == "sig-b64"

    def test_post_seller_still_retried_with_post(self):
        """The fix must not flip the common case: a seller that answers the
        POST probe with the 402 is paid over POST."""
        w = _session_wallet()
        w.base_address = "0x" + "b" * 40
        w.build_base_payment_signature.return_value = "sig-b64"
        s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
        with respx.mock:
            post = respx.post(self.URL).mock(side_effect=[
                httpx.Response(402, json=self._base_402()),
                httpx.Response(200, json={"ok": True}),
            ])
            out = s.call(self.URL, {})
        assert out["ok"] is True
        assert post.call_count == 2
