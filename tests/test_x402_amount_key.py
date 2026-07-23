"""
test_x402_amount_key.py — regression for the x402 amount-key bug.

The prober went 0/2 paid probes and exit(1) on 2026-07-23 with
    settle failed: evm:could not sign x402 payment: 'amount'
Root cause: the SDK read the price only from AgentPay's native `amount` key,
but standard x402 v2 payment-requirements use `maxAmountRequired`. A
standard-compliant seller's option was therefore priced at $0 (so it won the
"cheapest" selection) and then raised KeyError('amount') at signing.
Customer-facing: ANY agent paying such an external x402 URL failed — the
prober was just the canary.

`_x402_amount_atomic` is the single reader now used at all three sites
(build_base_payment_signature, discover(), _call_x402_url).
"""

import base64
import json
from decimal import Decimal

import pytest

from agentpay._wallet import _normalize_evm_network, _x402_amount_atomic


class TestX402AmountReader:

    def test_reads_agentpay_amount(self):
        assert _x402_amount_atomic({"amount": "1000"}) == 1000
        assert _x402_amount_atomic({"amount": 1000}) == 1000

    def test_reads_standard_maxAmountRequired(self):
        # The exact shape that broke the prober.
        assert _x402_amount_atomic({"maxAmountRequired": "1000"}) == 1000
        assert _x402_amount_atomic({"maxAmountRequired": 2500}) == 2500

    def test_amount_takes_precedence_when_both_present(self):
        assert _x402_amount_atomic(
            {"amount": "1000", "maxAmountRequired": "9999"}) == 1000

    def test_explicit_zero_amount_is_honoured(self):
        # A real free option: amount=0 must NOT fall through to maxAmountRequired.
        assert _x402_amount_atomic({"amount": 0, "maxAmountRequired": "9999"}) == 0
        assert _x402_amount_atomic({"amount": "0"}) == 0

    def test_blank_amount_falls_through(self):
        assert _x402_amount_atomic({"amount": "", "maxAmountRequired": "500"}) == 500

    def test_missing_both_is_none(self):
        assert _x402_amount_atomic({"scheme": "exact", "payTo": "0xabc"}) is None

    def test_malformed_is_none(self):
        assert _x402_amount_atomic({"amount": "not-a-number"}) is None
        assert _x402_amount_atomic({"maxAmountRequired": "1.5x"}) is None

    def test_maxAmountRequired_not_priced_at_zero(self):
        # The specific regression: a standard option must NOT read as $0.
        atomic = _x402_amount_atomic({"maxAmountRequired": "3000"})
        assert atomic == 3000
        assert Decimal(atomic) / Decimal("1000000") == Decimal("0.003")


class TestEvmNetworkNormalize:
    """The x402 signing lib requires CAIP-2 (eip155:CHAIN_ID); live services
    often advertise a friendly name ('base') — the prober's 2026-07-23
    failure #2 ('Unsupported network format: base')."""

    def test_friendly_base_maps_to_caip2(self):
        assert _normalize_evm_network("base") == "eip155:8453"
        assert _normalize_evm_network("Base") == "eip155:8453"
        assert _normalize_evm_network("base-mainnet") == "eip155:8453"

    def test_base_sepolia_maps(self):
        assert _normalize_evm_network("base-sepolia") == "eip155:84532"

    def test_already_caip2_passes_through(self):
        assert _normalize_evm_network("eip155:8453") == "eip155:8453"
        assert _normalize_evm_network("eip155:84532") == "eip155:84532"

    def test_blank_defaults_to_base_mainnet(self):
        assert _normalize_evm_network(None) == "eip155:8453"
        assert _normalize_evm_network("") == "eip155:8453"

    def test_unknown_passes_through_for_lib_to_validate(self):
        assert _normalize_evm_network("eip155:1") == "eip155:1"


class TestBuildBaseSignatureExternalCompat:
    """build_base_payment_signature must sign a standard external 402 —
    `maxAmountRequired` price AND a friendly `network: "base"` (the two
    prober failures). Needs the [base] extra (x402[evm]); skipped otherwise."""

    def _wallet(self):
        from stellar_sdk import Keypair

        from agentpay._wallet import AgentWallet
        return AgentWallet(
            secret_key=Keypair.random().secret, network="testnet",
            base_key="0x" + "11" * 32,   # valid throwaway EVM key
        )

    def test_signs_standard_maxAmountRequired_accept(self):
        pytest.importorskip("x402")
        w = self._wallet()
        accept = {
            "scheme": "exact",
            "network": "eip155:8453",
            "maxAmountRequired": "1000",     # standard key, NO `amount`
            "payTo": "0x" + "22" * 20,
            "asset": w.BASE_USDC,
            "maxTimeoutSeconds": 60,
        }
        header = w.build_base_payment_signature(accept, "https://svc.example/tool")
        decoded = json.loads(base64.b64decode(header))
        assert decoded["accepted"]["amount"] == "1000"

    def test_signs_friendly_network_base(self):
        # The exact prober failure #2: network='base' + maxAmountRequired.
        pytest.importorskip("x402")
        w = self._wallet()
        accept = {
            "scheme": "exact",
            "network": "base",               # friendly name, NOT CAIP-2
            "maxAmountRequired": "3000",
            "payTo": "0x" + "22" * 20,
            "asset": w.BASE_USDC,
            "maxTimeoutSeconds": 60,
        }
        # Before the fix: "Unsupported network format: base". Now it signs and
        # the accepted block carries the normalized CAIP-2 network.
        header = w.build_base_payment_signature(accept, "https://svc.example/tool")
        decoded = json.loads(base64.b64decode(header))
        assert decoded["accepted"]["network"] == "eip155:8453"
        assert decoded["accepted"]["amount"] == "3000"

    def test_missing_payto_raises_clear(self):
        pytest.importorskip("x402")
        w = self._wallet()
        accept = {"scheme": "exact", "network": "base", "maxAmountRequired": "1000"}
        with pytest.raises(KeyError, match="payTo"):
            w.build_base_payment_signature(accept, "https://svc.example/tool")
