"""
test_base.py — Tests for gateway/base.py CDP Mode A response validation.

Focused on the schema validation added in Tier 2 #19. Mode A is the
CDP-facilitator settlement path; CDP has occasionally returned
{"success": True} with missing or malformed transaction/payer/network
fields, and the gateway used to silently propagate those empty values
as receipts — making the paid call look successful even though the
on-chain proof was unrecoverable. This file pins the shape checks.

Mode B (direct on-chain via JSON-RPC) is exercised by the existing
production test wallet and is out of scope for these unit tests.
"""

import base64
import json

import httpx
import pytest
import respx

from gateway.base import settle_base_payment


# Canonical EVM-shaped values
VALID_TX_HASH = "0x" + "a" * 64           # 66 chars total — passes shape check
VALID_PAYER   = "0x" + "b" * 40           # 42 chars total — passes shape check
VALID_NETWORK = "eip155:8453"             # CAIP-2 Base mainnet
PAYTO         = "0x" + "c" * 40

CDP_URL = "https://api.cdp.coinbase.com/platform/v2/x402"


def _mode_a_signature_header() -> str:
    """A Mode A PAYMENT-SIGNATURE: base64-encoded JSON with no tx_hash key.

    The presence of `payload` (an EIP-3009 signature object) and absence
    of `tx_hash` is what tells settle_base_payment to take the CDP route
    instead of the direct on-chain Mode B route.
    """
    payload = {"payload": {"signature": "0xfake", "from": VALID_PAYER}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _payment_requirements() -> dict:
    return {
        "amount": "1000",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "network": VALID_NETWORK,
        "payTo": PAYTO,
    }


# ── Mode A schema validation (Tier 2 #19) ────────────────────────────────────

class TestModeASchemaValidation:

    @pytest.mark.asyncio
    async def test_happy_path(self):
        """Valid CDP response → success returned with all fields populated."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": True,
                    "transaction": VALID_TX_HASH,
                    "payer": VALID_PAYER,
                    "network": VALID_NETWORK,
                })
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is True
        assert result["tx_hash"] == VALID_TX_HASH
        assert result["payer"] == VALID_PAYER
        assert result["network"] == VALID_NETWORK
        assert result["reason"] == "ok"

    @pytest.mark.asyncio
    async def test_missing_transaction_key_rejected(self):
        """CDP returned success=true but no transaction key — fail closed."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": True,
                    # transaction key omitted
                    "payer": VALID_PAYER,
                    "network": VALID_NETWORK,
                })
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is False
        assert result["reason"] == "cdp_malformed_transaction"

    @pytest.mark.asyncio
    async def test_malformed_transaction_hash_rejected(self):
        """transaction is too short — must be 66 chars (0x + 64 hex)."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": True,
                    "transaction": "0xabc",                      # too short
                    "payer": VALID_PAYER,
                    "network": VALID_NETWORK,
                })
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is False
        assert result["reason"] == "cdp_malformed_transaction"

    @pytest.mark.asyncio
    async def test_transaction_missing_0x_prefix_rejected(self):
        """transaction is the right length but missing 0x prefix."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": True,
                    "transaction": "a" * 66,                     # no 0x
                    "payer": VALID_PAYER,
                    "network": VALID_NETWORK,
                })
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is False
        assert result["reason"] == "cdp_malformed_transaction"

    @pytest.mark.asyncio
    async def test_malformed_payer_rejected(self):
        """payer should be 0x + 40 hex (42 chars total)."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": True,
                    "transaction": VALID_TX_HASH,
                    "payer": "0xshort",                          # too short
                    "network": VALID_NETWORK,
                })
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is False
        assert result["reason"] == "cdp_malformed_payer"

    @pytest.mark.asyncio
    async def test_missing_network_rejected(self):
        """network field is required."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": True,
                    "transaction": VALID_TX_HASH,
                    "payer": VALID_PAYER,
                    # network key omitted
                })
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is False
        assert result["reason"] == "cdp_missing_network"

    @pytest.mark.asyncio
    async def test_empty_string_network_rejected(self):
        """network is present but an empty string — also reject."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": True,
                    "transaction": VALID_TX_HASH,
                    "payer": VALID_PAYER,
                    "network": "",
                })
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is False
        assert result["reason"] == "cdp_missing_network"


# ── Sanity: CDP failure path is unaffected ──────────────────────────────────

class TestModeAFailurePath:

    @pytest.mark.asyncio
    async def test_cdp_returns_success_false(self):
        """When CDP returns {success: false, errorReason: ...}, we propagate
        the error reason without going through the schema-validation path."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(200, json={
                    "success": False,
                    "errorReason": "insufficient_balance",
                })
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is False
        assert result["reason"] == "insufficient_balance"

    @pytest.mark.asyncio
    async def test_cdp_http_5xx(self):
        """CDP returned 502 → fail closed, don't try to parse JSON body."""
        with respx.mock:
            respx.post(f"{CDP_URL}/settle").mock(
                return_value=httpx.Response(502, text="Bad Gateway")
            )
            result = await settle_base_payment(
                _mode_a_signature_header(), _payment_requirements(),
            )
        assert result["success"] is False
        assert "facilitator_http_502" in result["reason"]


# ── Mode B replay-check cutover (PR #13e) ─────────────────────────────────────
#
# After cutover, the replay check on Base Mode B reads from Supabase first
# (composite PK on tx_hash, network) and falls back to the in-memory set if
# Supabase says "not consumed" or is unreachable. These tests pin both
# paths.

def _mode_b_signature_header(tx_hash: str, payer: str) -> str:
    """Mode B PAYMENT-SIGNATURE: base64-encoded JSON with tx_hash key.

    The presence of `tx_hash` is what tells settle_base_payment to take
    the direct-on-chain Mode B path instead of routing through CDP.
    """
    payload = {"tx_hash": tx_hash, "payer": payer}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class TestModeBReplayCutover:

    @pytest.mark.asyncio
    async def test_supabase_says_replay_rejects_before_rpc(self, monkeypatch):
        """If Supabase reports the tx_hash as already consumed on this
        network, Mode B must reject as replay BEFORE making the JSON-RPC
        eth_getTransactionReceipt call. Cheaper, and means a Railway
        redeploy that wipes _used_base_tx_hashes can still catch replays.
        """
        import gateway.base as base_mod
        monkeypatch.setattr(base_mod.sb, "sb_enabled", lambda: True)

        rpc_calls = []
        async def fake_rpc(*args, **kwargs):
            rpc_calls.append(1)
            raise AssertionError("verify_base_tx should not have been called")
        monkeypatch.setattr(base_mod, "verify_base_tx", fake_rpc)

        async def supabase_says_consumed(tx, net):
            assert net == "base-mainnet"
            return True
        monkeypatch.setattr(
            base_mod.sb, "is_tx_hash_consumed", supabase_says_consumed
        )

        result = await settle_base_payment(
            _mode_b_signature_header(VALID_TX_HASH, VALID_PAYER),
            _payment_requirements(),
        )
        assert result["success"] is False
        assert result["reason"] == "replay_attack"
        assert rpc_calls == []  # short-circuited before RPC

    @pytest.mark.asyncio
    async def test_supabase_clean_falls_through_to_rpc(self, monkeypatch):
        """When Supabase says not-consumed and in-memory has no record,
        Mode B proceeds with the JSON-RPC receipt check normally.
        """
        import gateway.base as base_mod
        monkeypatch.setattr(base_mod.sb, "sb_enabled", lambda: True)

        # Make sure in-memory set is empty so we're testing the
        # Supabase-clean-AND-memory-clean path
        base_mod._used_base_tx_hashes.discard(VALID_TX_HASH)

        async def supabase_says_clean(tx, net):
            return False
        async def supabase_record(tx, net):
            return True
        monkeypatch.setattr(
            base_mod.sb, "is_tx_hash_consumed", supabase_says_clean
        )
        monkeypatch.setattr(
            base_mod.sb, "record_tx_hash", supabase_record
        )

        async def fake_verify(**kwargs):
            return {
                "success": True, "tx_hash": kwargs["tx_hash"],
                "payer": kwargs["payer"], "network": "", "reason": "ok",
            }
        monkeypatch.setattr(base_mod, "verify_base_tx", fake_verify)

        result = await settle_base_payment(
            _mode_b_signature_header(VALID_TX_HASH, VALID_PAYER),
            _payment_requirements(),
        )
        assert result["success"] is True
        assert result["tx_hash"] == VALID_TX_HASH
        # In-memory set was updated as a cache for graceful degradation
        assert VALID_TX_HASH in base_mod._used_base_tx_hashes


# ── PAYMENT-REQUIRED outputSchema (Bazaar indexing) ───────────────────────────
#
# Coinbase's Bazaar reads `accepts[0].outputSchema` from the PAYMENT-REQUIRED
# header on the first Base mainnet payment through the CDP facilitator to
# auto-index the tool. Without it the listing shows price but no shape.
# These tests pin the header shape and the non-mutation invariant on the
# caller's requirements dict.

from gateway.base import build_payment_required_header, build_payment_requirements


def _sample_requirements() -> dict:
    return build_payment_requirements(
        amount_usdc="0.001",
        pay_to=PAYTO,
        resource_url="https://agentpay.tools/tools/token_price",
        network="base-mainnet",
    )


def _sample_output_schema() -> dict:
    return {
        "input": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
        "output": {"symbol": "ETH", "price_usd": 2069.73, "source": "coingecko"},
    }


class TestPaymentRequiredHeader:

    def test_output_schema_embedded_in_accepts_entry(self):
        """outputSchema lives inside accepts[0], not at payload top level —
        this is the shape Bazaar's indexer reads."""
        header = build_payment_required_header(
            requirements=_sample_requirements(),
            resource_url="https://agentpay.tools/tools/token_price",
            tool_description="Get current USD price",
            output_schema=_sample_output_schema(),
        )
        decoded = json.loads(base64.b64decode(header))
        assert decoded["x402Version"] == 2
        assert "outputSchema" in decoded["accepts"][0]
        assert decoded["accepts"][0]["outputSchema"]["input"]["properties"]["symbol"]["type"] == "string"
        assert decoded["accepts"][0]["outputSchema"]["output"]["price_usd"] == 2069.73

    def test_output_schema_omitted_when_none(self):
        """Backward compat: callers that don't pass output_schema get a
        payload with no outputSchema key — same shape as before the patch."""
        header = build_payment_required_header(
            requirements=_sample_requirements(),
            resource_url="https://agentpay.tools/tools/token_price",
            tool_description="Get current USD price",
        )
        decoded = json.loads(base64.b64decode(header))
        assert "outputSchema" not in decoded["accepts"][0]

    def test_caller_requirements_dict_not_mutated(self):
        """The function does a shallow copy of `requirements` before embedding
        outputSchema. The same dict is reused as input to settle_base_payment,
        so leaking outputSchema into it would pollute the CDP /settle call."""
        req = _sample_requirements()
        original_keys = set(req.keys())
        build_payment_required_header(
            requirements=req,
            resource_url="x",
            tool_description="y",
            output_schema=_sample_output_schema(),
        )
        assert set(req.keys()) == original_keys
        assert "outputSchema" not in req

    def test_payload_is_valid_base64_json(self):
        """Cheap smoke test — corruption here would silently break every
        x402-v2-aware client, not just Bazaar."""
        header = build_payment_required_header(
            requirements=_sample_requirements(),
            resource_url="x",
            tool_description="y",
            output_schema=_sample_output_schema(),
        )
        # Round-trip without error
        decoded = json.loads(base64.b64decode(header))
        assert isinstance(decoded, dict)
        assert decoded["error"] == "Payment required"
