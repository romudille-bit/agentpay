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
        # AGE-90: `accepted` echoes the seller's entry VERBATIM — their key
        # (maxAmountRequired), their values. The signed authorization still
        # carries the parsed amount.
        assert decoded["accepted"] == accept
        assert decoded["payload"]["authorization"]["value"] == "1000"

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
        # Before the AGE-78 fix: "Unsupported network format: base". Now it
        # signs (CAIP-2 normalization feeds the SIGNER), while the accepted
        # echo keeps the seller's own vocabulary — a strict matcher compares
        # it against what THEY advertised, so 'base' must stay 'base' (AGE-90).
        header = w.build_base_payment_signature(accept, "https://svc.example/tool")
        decoded = json.loads(base64.b64decode(header))
        assert decoded["accepted"]["network"] == "base"
        assert decoded["accepted"] == accept
        assert decoded["payload"]["authorization"]["value"] == "3000"

    def test_missing_payto_raises_clear(self):
        pytest.importorskip("x402")
        w = self._wallet()
        accept = {"scheme": "exact", "network": "base", "maxAmountRequired": "1000"}
        with pytest.raises(KeyError, match="payTo"):
            w.build_base_payment_signature(accept, "https://svc.example/tool")


class TestAcceptedEchoAGE90:
    """AGE-90: the accepted block must echo the seller's accepts entry
    VERBATIM. Strict v2 middlewares deep-compare it against what they
    advertised; our injected resource/mimeType keys made 7 sellers answer
    every paid retry with 'No matching payment requirements' → fresh 402 {}
    (root-caused live against ApiToll/Otto, 2026-07-28)."""

    def _wallet(self):
        from stellar_sdk import Keypair
        from agentpay._wallet import AgentWallet
        return AgentWallet(secret_key=Keypair.random().secret, network="testnet",
                           base_key="0x" + "11" * 32)

    def _decode(self, accept):
        w = self._wallet()
        header = w.build_base_payment_signature(accept, "https://svc.example/tool")
        return json.loads(base64.b64decode(header))

    def test_no_injected_keys(self):
        pytest.importorskip("x402")
        accept = {"scheme": "exact", "network": "eip155:8453", "amount": "1000",
                  "payTo": "0x" + "22" * 20,
                  "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                  "maxTimeoutSeconds": 300,
                  "extra": {"name": "USD Coin", "version": "2"}}
        decoded = self._decode(accept)
        assert "resource" not in decoded["accepted"]
        assert "mimeType" not in decoded["accepted"]
        assert decoded["accepted"] == accept

    def test_seller_extras_survive_the_echo(self):
        # Some sellers put outputSchema or custom keys on the accepts entry;
        # a deep-equality matcher wants those back too.
        pytest.importorskip("x402")
        accept = {"scheme": "exact", "network": "eip155:8453", "amount": "1000",
                  "payTo": "0x" + "22" * 20,
                  "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                  "maxTimeoutSeconds": 300, "outputSchema": {"type": "object"},
                  "custom": ["x", 1]}
        decoded = self._decode(accept)
        assert decoded["accepted"] == accept

    def test_echo_is_a_copy_not_a_reference(self):
        pytest.importorskip("x402")
        accept = {"scheme": "exact", "network": "eip155:8453", "amount": "1000",
                  "payTo": "0x" + "22" * 20,
                  "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                  "maxTimeoutSeconds": 300}
        decoded = self._decode(accept)
        assert decoded["accepted"] == accept   # snapshot equal
        # ...and the builder must not have mutated the caller's dict
        assert set(accept) == {"scheme", "network", "amount", "payTo", "asset",
                               "maxTimeoutSeconds"}

    def test_timeout_clamp_still_applies_to_the_signature(self):
        # A hostile year-long maxTimeoutSeconds is echoed verbatim (that is
        # the seller's own claim) but the SIGNED validBefore stays clamped —
        # AGE-67 unchanged by the echo.
        pytest.importorskip("x402")
        import time
        accept = {"scheme": "exact", "network": "eip155:8453", "amount": "1000",
                  "payTo": "0x" + "22" * 20,
                  "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                  "maxTimeoutSeconds": 31_536_000}
        decoded = self._decode(accept)
        assert decoded["accepted"]["maxTimeoutSeconds"] == 31_536_000
        valid_before = int(decoded["payload"]["authorization"]["validBefore"])
        assert valid_before <= time.time() + 601   # MAX_AUTH_VALIDITY_SECONDS
