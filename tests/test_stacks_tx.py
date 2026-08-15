"""
test_stacks_tx.py — AGE-22: the minimal Python Stacks signing lib.

Oracle: fixtures generated with @stacks/transactions v7 (stacks.js, the
canonical Stacks tooling) by tools/gen_stacks_fixtures.mjs. Both sides use
RFC6979 deterministic ECDSA, so signed transactions must match the stacks.js
output BYTE-FOR-BYTE — the strongest possible serialization test short of a
live node. txids must match stacks.js txids (sha512/256 of the signed tx).

Regenerate fixtures (requires node):
    cd tools && npm install @stacks/transactions c32check
    node gen_stacks_fixtures.mjs   # writes tests/fixtures/stacks_tx_fixtures.json

Checklist coverage (docs/stacks-adapter.md):
  #5 — the memo arg carries payment_id; building without one is impossible
  #8 — key parsing raises a constant message; the secret never leaks
  post-conditions — mandatory, deny-mode, appended by the builder itself
  pre-broadcast txid — computable before broadcast, matches stacks.js
  sponsored variant — origin-signed wire format matches stacks.js exactly
"""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from agentpay._stacks_tx import (
    PostCondition,
    SBTC_CONTRACT_MAINNET,
    SBTC_CONTRACT_TESTNET,
    StacksKeypair,
    build_sbtc_transfer,
    c32_address,
    c32_decode,
    sats_from_usd,
    sign_transaction,
    txid_of,
)
import agentpay._stacks_tx as stacks_tx

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "stacks_tx_fixtures.json").read_text()
)

INVALID_KEY_MSG = "invalid Stacks private key"


def _keypair_for(t: dict) -> StacksKeypair:
    key = next(
        k for k in FIXTURES["keys"] if k[f"address_{t['network']}"] == t["sender"]
    )
    return StacksKeypair.from_secret(key["private_key"])


def _build(t: dict) -> bytes:
    return build_sbtc_transfer(
        sender=_keypair_for(t),
        recipient=t["recipient"],
        amount_sats=int(t["amount_sats"]),
        payment_id=t["payment_id"],
        nonce=int(t["nonce"]),
        fee_microstx=int(t["fee"]),
        network=t["network"],
        sponsored=t["sponsored"],
        # Vectors are byte-exact against the contract they were generated with;
        # pin it so the live default can move with testnet redeployments.
        contract=t["contract"],
    )


# ---------------------------------------------------------------- c32check


class TestC32:
    @pytest.mark.parametrize(
        "vec", FIXTURES["c32_vectors"], ids=lambda v: f"v{v['version']}-{v['hash160'][:8]}"
    )
    def test_encode_matches_stacksjs(self, vec):
        assert c32_address(vec["version"], bytes.fromhex(vec["hash160"])) == vec["address"]

    @pytest.mark.parametrize(
        "vec", FIXTURES["c32_vectors"], ids=lambda v: f"v{v['version']}-{v['hash160'][:8]}"
    )
    def test_decode_roundtrip(self, vec):
        version, h160 = c32_decode(vec["address"])
        assert version == vec["version"]
        assert h160.hex() == vec["hash160"]

    def test_decode_normalizes_ambiguous_chars(self):
        # Crockford: lowercase folds up, O→0, L/I→1.
        addr = FIXTURES["c32_vectors"][0]["address"]
        assert c32_decode(addr.lower()) == c32_decode(addr)
        mangled = "S" + addr[1:].replace("0", "O", 1)
        assert c32_decode(mangled) == c32_decode(addr)

    def test_decode_rejects_bad_checksum(self):
        addr = FIXTURES["c32_vectors"][-1]["address"]
        # flip the last character to break the checksum
        bad_last = "2" if addr[-1] != "2" else "3"
        with pytest.raises(ValueError):
            c32_decode(addr[:-1] + bad_last)

    def test_decode_rejects_garbage(self):
        for bad in ("", "SP", "hello", "SPU*", "XP2ZD731ANQZT6J4K3F5N8A40ZXWXC1XFXHVVQFKE"):
            with pytest.raises(ValueError):
                c32_decode(bad)

    def test_encode_validates_inputs(self):
        with pytest.raises(ValueError):
            c32_address(32, b"\x00" * 20)  # version out of range
        with pytest.raises(ValueError):
            c32_address(22, b"\x00" * 19)  # not a hash160


# ---------------------------------------------------------------- keys


class TestStacksKeypair:
    @pytest.mark.parametrize(
        "key", FIXTURES["keys"], ids=lambda k: k["private_key"][:8]
    )
    def test_pubkey_and_addresses_match_stacksjs(self, key):
        kp = StacksKeypair.from_secret(key["private_key"])
        assert kp.public_key().hex() == key["public_key"]
        assert kp.address("mainnet") == key["address_mainnet"]
        assert kp.address("testnet") == key["address_testnet"]

    def test_compressed_flag_follows_stacks_convention(self):
        raw = "b244296d5907de9864c0b0d51f98a13c52890be0404e83f273144cd5b9960eed"
        assert StacksKeypair.from_secret(raw + "01").compressed is True
        assert StacksKeypair.from_secret(raw).compressed is False
        # 0x prefix tolerated
        assert StacksKeypair.from_secret("0x" + raw + "01").compressed is True

    @pytest.mark.parametrize(
        "bad_secret",
        [
            "",  # empty
            "abc",  # short
            "zz" * 33,  # not hex
            "b244296d5907de9864c0b0d51f98a13c52890be0404e83f273144cd5b9960eed" + "02",  # 66 hex, wrong suffix
            "00" * 32,  # zero scalar
            "ff" * 32,  # >= group order
            "b2" * 40,  # wrong length
        ],
        ids=["empty", "short", "nonhex", "bad-suffix", "zero", "over-order", "long"],
    )
    def test_invalid_keys_raise_constant_message(self, bad_secret):
        # [CHECKLIST #8]: exact constant message, no interpolation, no chain.
        with pytest.raises(ValueError) as exc_info:
            StacksKeypair.from_secret(bad_secret)
        assert str(exc_info.value) == INVALID_KEY_MSG
        assert exc_info.value.__cause__ is None
        if bad_secret:
            assert bad_secret not in repr(exc_info.value)

    def test_secret_never_in_error_text(self):
        secret = "deadbeef" * 8 + "0301"  # invalid length: 66+2
        try:
            StacksKeypair.from_secret(secret)
        except ValueError as e:
            assert "deadbeef" not in str(e) and "deadbeef" not in repr(e)
        else:  # pragma: no cover
            pytest.fail("expected ValueError")

    def test_address_rejects_unknown_network(self):
        kp = StacksKeypair.from_secret(FIXTURES["keys"][0]["private_key"])
        with pytest.raises(ValueError):
            kp.address("devnet")


# ---------------------------------------------------------------- build


class TestBuildSbtcTransfer:
    def test_unsigned_serialization_matches_stacksjs(self):
        p = FIXTURES["presign"]
        kp = StacksKeypair.from_secret(p["private_key"])
        unsigned = build_sbtc_transfer(
            sender=kp,
            recipient=p["recipient"],
            amount_sats=int(p["amount_sats"]),
            payment_id=p["payment_id"],
            nonce=int(p["nonce"]),
            fee_microstx=int(p["fee"]),
            network="testnet",
            contract=p["contract"],
        )
        assert unsigned.hex() == p["unsigned_serialized_hex"]

    def test_payment_id_required(self):
        # [CHECKLIST #5]: the challenge binding is not optional in this lib.
        t = FIXTURES["transactions"][0]
        with pytest.raises(ValueError, match="payment_id"):
            build_sbtc_transfer(
                sender=_keypair_for(t),
                recipient=t["recipient"],
                amount_sats=100,
                payment_id="",
                nonce=0,
                fee_microstx=200,
                network="testnet",
            )

    def test_memo_truncated_to_34_bytes(self):
        t = FIXTURES["transactions"][0]
        kp = _keypair_for(t)
        common = dict(
            sender=kp, recipient=t["recipient"], amount_sats=100,
            nonce=0, fee_microstx=200, network="testnet",
        )
        long_id = "pay_" + "a" * 60
        tx_long = build_sbtc_transfer(payment_id=long_id, **common)
        tx_34 = build_sbtc_transfer(payment_id=long_id[:34], **common)
        assert tx_long == tx_34
        # and the 34-byte memo bytes are present in the payload
        assert long_id[:34].encode() in tx_long

    def test_deny_mode_post_condition_always_present(self):
        # Post-conditions are mandatory hygiene: there is no code path that
        # builds a transfer without the deny-mode sent-equal post-condition.
        t = FIXTURES["transactions"][0]
        tx = _build(t)
        pc = PostCondition(
            sender=t["sender"], contract=t["contract"],
            amount_sats=int(t["amount_sats"]),
        ).serialize()
        assert pc in tx
        # deny mode byte (0x02) directly before the 4-byte PC count + PC
        pc_block = b"\x02" + (1).to_bytes(4, "big") + pc
        assert pc_block in tx

    def test_input_validation(self):
        t = FIXTURES["transactions"][0]
        kp = _keypair_for(t)
        base = dict(
            sender=kp, recipient=t["recipient"], payment_id="pay_x",
            nonce=0, fee_microstx=200,
        )
        with pytest.raises(ValueError):
            build_sbtc_transfer(amount_sats=0, network="testnet", **base)
        with pytest.raises(ValueError):
            build_sbtc_transfer(amount_sats=-5, network="testnet", **base)
        with pytest.raises(ValueError):
            build_sbtc_transfer(amount_sats=10, network="devnet", **base)
        with pytest.raises(ValueError):
            build_sbtc_transfer(
                amount_sats=10, network="testnet",
                **{**base, "nonce": -1},
            )

    def test_network_selects_sbtc_contract(self):
        t_test = FIXTURES["transactions"][0]  # testnet
        t_main = FIXTURES["transactions"][1]  # mainnet

        def _build_default(t):  # no contract pin: exercise network resolution
            return build_sbtc_transfer(
                sender=_keypair_for(t), recipient=t["recipient"],
                amount_sats=int(t["amount_sats"]), payment_id=t["payment_id"],
                nonce=int(t["nonce"]), fee_microstx=int(t["fee"]),
                network=t["network"], sponsored=t["sponsored"],
            )

        assert SBTC_CONTRACT_TESTNET.split(".")[0].encode("ascii") not in _build_default(t_main)
        # contract address is serialized as version+hash160, so check via decode
        _, test_h160 = c32_decode(SBTC_CONTRACT_TESTNET.split(".")[0])
        _, main_h160 = c32_decode(SBTC_CONTRACT_MAINNET.split(".")[0])
        assert test_h160 in _build_default(t_test)
        assert main_h160 in _build_default(t_main)
        assert main_h160 not in _build_default(t_test)


# ---------------------------------------------------------------- sign + txid


class TestSignTransaction:
    @pytest.mark.parametrize(
        "t", FIXTURES["transactions"], ids=lambda t: t["name"]
    )
    def test_signed_tx_matches_stacksjs_byte_for_byte(self, t):
        kp = _keypair_for(t)
        signed = sign_transaction(_build(t), kp)
        assert signed.hex() == t["serialized_hex"]

    @pytest.mark.parametrize(
        "t", FIXTURES["transactions"], ids=lambda t: t["name"]
    )
    def test_txid_matches_stacksjs(self, t):
        kp = _keypair_for(t)
        signed = sign_transaction(_build(t), kp)
        assert txid_of(signed) == t["txid"]

    def test_presign_sighash_chain_matches_stacksjs(self):
        # Validates the intermediate signing values, not just the end state:
        # signBegin (cleared-auth tx hash) and the presign sighash.
        p = FIXTURES["presign"]
        kp = StacksKeypair.from_secret(p["private_key"])
        unsigned = bytes.fromhex(p["unsigned_serialized_hex"])

        cleared = stacks_tx._serialize_spending_condition(
            kp.signer_hash160(), 0, 0, 0, b"\x00" * 65
        )
        sign_begin = stacks_tx._sha512_256(
            stacks_tx._with_origin_condition(unsigned, cleared)
        )
        assert sign_begin.hex() == p["sign_begin"]

        presign = stacks_tx._sha512_256(
            sign_begin
            + bytes([0x04])
            + int(p["fee"]).to_bytes(8, "big")
            + int(p["nonce"]).to_bytes(8, "big")
        )
        assert presign.hex() == p["presign_sighash"]

        signed = sign_transaction(unsigned, kp)
        assert signed.hex() == p["signed_serialized_hex"]
        assert txid_of(signed) == p["txid"]

    def test_wrong_keypair_rejected(self):
        t = FIXTURES["transactions"][0]
        other = StacksKeypair.from_secret(FIXTURES["keys"][2]["private_key"])
        with pytest.raises(ValueError, match="does not match"):
            sign_transaction(_build(t), other)

    def test_malformed_tx_rejected(self):
        kp = StacksKeypair.from_secret(FIXTURES["keys"][0]["private_key"])
        with pytest.raises(ValueError):
            sign_transaction(b"\x80\x80\x00\x00\x00\x99", kp)
        with pytest.raises(ValueError):
            sign_transaction(b"", kp)

    def test_txid_computable_pre_broadcast(self):
        # txid is a pure function of the signed bytes (sha512/256) — the
        # gateway's replay-consume can key on it BEFORE any broadcast.
        t = FIXTURES["transactions"][0]
        signed = sign_transaction(_build(t), _keypair_for(t))
        expected = hashlib.new("sha512_256", signed).hexdigest()
        assert txid_of(signed) == expected == t["txid"]

    def test_signing_is_deterministic(self):
        # RFC6979: same tx + same key → same signature, every time.
        t = FIXTURES["transactions"][0]
        kp = _keypair_for(t)
        assert sign_transaction(_build(t), kp) == sign_transaction(_build(t), kp)

    def test_sponsored_wire_format(self):
        # Sponsored variant: auth type 0x05 + the stacks.js sponsor
        # placeholder (hash160 of 33 zero bytes), relay fills it later.
        t = next(x for x in FIXTURES["transactions"] if x["sponsored"])
        signed = sign_transaction(_build(t), _keypair_for(t))
        assert signed[5] == 0x05
        placeholder_signer = signed[110:130]
        sha = hashlib.sha256(bytes(33)).digest()
        assert placeholder_signer == stacks_tx._hash160(bytes(33))[:20]
        assert signed.hex() == t["serialized_hex"]


# ---------------------------------------------------------------- hashing


class TestHashing:
    def test_ripemd160_fallback_matches_openssl(self):
        try:
            hashlib.new("ripemd160")
        except ValueError:
            pytest.skip("OpenSSL ripemd160 unavailable; fallback is the only path")
        import os

        for size in (0, 1, 20, 33, 55, 56, 64, 65, 200):
            data = os.urandom(size)
            assert (
                stacks_tx._ripemd160_pure(data)
                == hashlib.new("ripemd160", data).digest()
            )

    def test_hash160_of_known_pubkey(self):
        # hash160(compressed pubkey of k=1) — the classic secp256k1 vector.
        kp = StacksKeypair.from_secret("00" * 31 + "01" + "01")
        assert kp.signer_hash160().hex() == "751e76e8199196d454941c45d1b3a323f1433bd6"


# ---------------------------------------------------------------- pricing stub


class TestSatsFromUsd:
    def test_exact_conversion(self):
        # $0.01 at $100k/BTC = 10 sats exactly
        assert sats_from_usd(Decimal("0.01"), Decimal("100000")) == 10

    def test_ceils_never_undercharges(self):
        # $0.01 at $97,123/BTC = 10.296... sats → 11, never 10
        sats = sats_from_usd(Decimal("0.01"), Decimal("97123"))
        assert sats == 11
        assert (Decimal(sats) / Decimal(100_000_000)) * Decimal("97123") >= Decimal("0.01")

    def test_zero_usd_is_zero_sats(self):
        assert sats_from_usd(Decimal("0"), Decimal("100000")) == 0

    def test_validation(self):
        with pytest.raises(ValueError):
            sats_from_usd(Decimal("1"), Decimal("0"))
        with pytest.raises(ValueError):
            sats_from_usd(Decimal("-1"), Decimal("100000"))
