"""
test_unsupported_chain.py — AGE-80.

The prober kept meeting live paid x402 sellers on chains our Base/Stellar
wallet can't settle (Avalanche C-Chain eip155:43114, 2026-07-23 sweep:
deepseek.x402.press, news.x402.press). The old _chain_kind() treated ANY
eip155:* as Base, so the SDK signed a doomed Base USDC authorization (real
spend, never confirmed) and the seller was mis-scored as a delivery failure.

These sellers must instead be reported as UNSUPPORTED — unmet demand on a
chain we don't settle, not a fault. This file locks in:
  * _is_base_settleable — only Base 8453/84532 (+ friendly aliases) are True
  * UnsupportedChainPayment — subclasses PaymentFailed, carries offered_networks
  * build_base_payment_signature refuses non-settleable chains (never signs)
  * the prober's score() never counts an unsupported-chain (skipped) row
"""

import base64
import json

import pytest

from agentpay._wallet import (
    PaymentFailed,
    UnsupportedChainPayment,
    _BASE_SETTLEABLE_CAIP2,
    _is_base_settleable,
)


class TestIsBaseSettleable:

    def test_base_mainnet_variants(self):
        assert _is_base_settleable("base")
        assert _is_base_settleable("Base")
        assert _is_base_settleable("base-mainnet")
        assert _is_base_settleable("eip155:8453")

    def test_base_sepolia(self):
        assert _is_base_settleable("base-sepolia")
        assert _is_base_settleable("eip155:84532")

    def test_non_base_evm_is_not_settleable(self):
        assert not _is_base_settleable("eip155:43114")   # Avalanche C-Chain
        assert not _is_base_settleable("eip155:42161")   # Arbitrum One
        assert not _is_base_settleable("eip155:137")     # Polygon
        assert not _is_base_settleable("eip155:1")       # Ethereum L1

    def test_blank_and_junk(self):
        assert not _is_base_settleable("")
        assert not _is_base_settleable(None)
        assert not _is_base_settleable("solana:mainnet")

    def test_settleable_set_is_exactly_base(self):
        assert _BASE_SETTLEABLE_CAIP2 == frozenset({"eip155:8453", "eip155:84532"})


class TestUnsupportedChainPaymentException:

    def test_subclasses_payment_failed(self):
        # Existing `except PaymentFailed` handlers (incl. the prober's) must
        # keep catching it, so this stays a graceful skip, not a crash.
        assert issubclass(UnsupportedChainPayment, PaymentFailed)

    def test_carries_offered_networks(self):
        e = UnsupportedChainPayment(
            "nope", offered_networks=["eip155:43114"], settleable=["base", "stellar"])
        assert e.offered_networks == ["eip155:43114"]
        assert e.settleable == ["base", "stellar"]

    def test_defaults_empty(self):
        e = UnsupportedChainPayment("nope")
        assert e.offered_networks == []
        assert e.settleable == []

    def test_exported_from_package(self):
        import agentpay
        assert agentpay.UnsupportedChainPayment is UnsupportedChainPayment


class TestBuildBaseGuardRefusesUnsettleable:
    """build_base_payment_signature must refuse to sign a Base auth for a
    non-settleable chain (defense in depth) but still sign for Base. Needs the
    [base] extra (x402[evm]) to reach the signer path; skipped otherwise."""

    def _wallet(self):
        from stellar_sdk import Keypair

        from agentpay._wallet import AgentWallet
        return AgentWallet(
            secret_key=Keypair.random().secret, network="testnet",
            base_key="0x" + "11" * 32,
        )

    def test_refuses_avalanche(self):
        pytest.importorskip("x402")
        w = self._wallet()
        accept = {
            "scheme": "exact",
            "network": "eip155:43114",       # Avalanche — not settleable
            "maxAmountRequired": "1000",
            "payTo": "0x" + "22" * 20,
            "asset": w.BASE_USDC,
        }
        with pytest.raises(UnsupportedChainPayment) as ei:
            w.build_base_payment_signature(accept, "https://svc.example/tool")
        assert "eip155:43114" in ei.value.offered_networks

    def test_still_signs_base(self):
        pytest.importorskip("x402")
        w = self._wallet()
        accept = {
            "scheme": "exact",
            "network": "base",
            "maxAmountRequired": "1000",
            "payTo": "0x" + "22" * 20,
            "asset": w.BASE_USDC,
        }
        header = w.build_base_payment_signature(accept, "https://svc.example/tool")
        decoded = json.loads(base64.b64decode(header))
        assert decoded["accepted"]["network"] == "eip155:8453"


class TestProberScoreIgnoresUnsupported:
    """An unsupported-chain probe row is skipped → it must never produce a
    delivery_rate (the seller is unreachable by us, not a bad deliverer)."""

    def test_unsupported_chain_row_is_unscoreable(self):
        from agents.prober import probe
        rows = [{
            "resource_url": "https://avax.example/x",
            "probe_type": "paid",
            "skipped": True,
            "unsupported_chain": ["eip155:43114"],
            "probed_at": "2026-07-23T12:00:00+00:00",
        }]
        scores = probe.score(rows)
        # Grouped but unscoreable: no paid probe counted, rate stays None,
        # factor stays neutral (never the 0.25 delivery-failure penalty).
        assert all(s["paid_probes"] == 0 for s in scores)
        assert all(s["delivery_rate"] is None for s in scores)
        assert all(s["delivery_factor"] == 1.0 for s in scores)
