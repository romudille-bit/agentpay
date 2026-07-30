"""
_stacks_tx.py — Minimal Python Stacks transaction signing (AGE-22).

STATUS: IMPLEMENTED (2026-07-20). Pure/no-I/O; nothing in the SDK imports it
until AGE-25 wires `chain="stacks"`. Design doc: docs/stacks-adapter.md
(read the 12-point checklist first).

The long pole of the Stacks adapter: no mature Python Stacks signing library
exists (canonical tooling is stacks.js), so this is a minimal, spec-documented
implementation of exactly what the x402 payment path needs — a signed SIP-010
`sbtc-token::transfer` contract call — and nothing else.

Specs: SIP-005 (transaction encoding), SIP-010 (fungible token trait),
Hiro API (`POST /v2/transactions`, `GET /extended/v1/tx/{txid}`).
Curve: secp256k1 — same as EVM; primitives come from `eth_keys`, a CORE
dependency (its pure-python NativeECCBackend needs no coincurve; RFC6979
deterministic signatures, so output is byte-identical to stacks.js/noble for
the same inputs).

Fixture-validated against @stacks/transactions v7 output
(tests/fixtures/stacks_tx_fixtures.json, generator: tools/gen_stacks_fixtures.mjs):
serialized txs match byte-for-byte, txids match. Also live-validated against a
Stacks testnet node (2026-07-20): `POST /v2/transactions` on api.testnet.hiro.so
fully deserialized a tx built here and rejected it only for NotEnoughFunds
(unfunded test key), with the node's txid equal to our pre-broadcast txid_of().

Non-goals (explicitly out of scope here):
  - broadcasting (gateway/stacks.py owns that; this lib is pure/no-I/O)
  - facilitator or relay HTTP integration
  - any budget logic (lives in _wallet.py — checklist items 1-4, 11-12)

Wire-format requirements this lib MUST satisfy (from the review checklist):
  [CHECKLIST #5]  the transfer's optional `(buff 34)` memo arg carries
                  payment_id — the challenge binding (Stellar-memo analog).
                  Amount-only binding was the AGE-64 hole; never rely on it.
  [CHECKLIST #8]  key parsing raises a CONSTANT message ("invalid Stacks
                  private key") — never interpolate the key or the underlying
                  exception text into errors.
  - post-conditions are MANDATORY hygiene: every transfer asserts "exactly N
    sats of sbtc-token leave sender" — this is what makes a signed tx safe to
    hand to an untrusted facilitator. Wrong/absent post-conditions = unsafe.
    `build_sbtc_transfer` appends the deny-mode post-condition itself; there
    is deliberately no way to build a transfer without one.
  - txid must be computable BEFORE broadcast (sha512/256 of the signed tx) —
    the gateway's atomic replay-consume keys on it pre-settle.
  - the sponsored-transaction auth variant must be representable (client signs
    with sponsored flag; relay co-signs and pays the STX fee) so the aibtcdev
    sponsor-relay path isn't precluded. Building the relay flow is NOT in
    scope — only not breaking the wire format for it.

Acceptance (AGE-22): a tx built here is accepted by a Stacks testnet node;
serialization unit-tested against fixtures generated with stacks.js; txid
matches the node's txid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from eth_keys import keys as _eth_keys

__all__ = [
    "StacksKeypair",
    "PostCondition",
    "build_sbtc_transfer",
    "sign_transaction",
    "txid_of",
    "c32_address",
    "c32_decode",
]

# CAIP-2 ids for the x402 `network` field (lowercase header dialect).
STACKS_MAINNET_CAIP2 = "stacks:1"
STACKS_TESTNET_CAIP2 = "stacks:2147483648"

# SIP-010 sBTC token contracts (principal.contract-name).
SBTC_CONTRACT_MAINNET = "SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token"
SBTC_CONTRACT_TESTNET = "ST1F7QA2MDF17S807EPA36TSS8AMEFY4KA9TVGWXT.sbtc-token"

# SIP-010 asset name of the sBTC fungible token (used in post-conditions).
SBTC_ASSET_NAME = "sbtc-token"

_INVALID_KEY_MSG = "invalid Stacks private key"  # [CHECKLIST #8] constant, never interpolate

# secp256k1 group order (key validity bound: 0 < d < N).
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# --- SIP-005 wire constants -------------------------------------------------

_TX_VERSION = {"mainnet": 0x00, "testnet": 0x80}
_CHAIN_ID = {"mainnet": 0x00000001, "testnet": 0x80000000}

_AUTH_STANDARD = 0x04
_AUTH_SPONSORED = 0x05

_HASH_MODE_P2PKH = 0x00  # single-sig, hash160(pubkey)

_KEY_ENCODING_COMPRESSED = 0x00
_KEY_ENCODING_UNCOMPRESSED = 0x01

_ANCHOR_MODE_ANY = 0x03
_PC_MODE_DENY = 0x02

_PC_TYPE_FUNGIBLE = 0x01
_PC_PRINCIPAL_STANDARD = 0x02
_FT_CONDITION_CODES = {
    "sent_equal_to": 0x01,
    "sent_greater_than": 0x02,
    "sent_greater_equal": 0x03,
    "sent_less_than": 0x04,
    "sent_less_equal": 0x05,
}

_PAYLOAD_CONTRACT_CALL = 0x02

# Clarity value type prefixes (SIP-005 §Clarity value representation).
_CV_UINT = 0x01
_CV_BUFFER = 0x02
_CV_PRINCIPAL_STANDARD = 0x05
_CV_OPTIONAL_NONE = 0x09
_CV_OPTIONAL_SOME = 0x0A

# c32 address versions (c32check): single-sig P2PKH.
_ADDR_VERSION_P2PKH = {"mainnet": 22, "testnet": 26}

_MEMO_MAX_BYTES = 34  # (buff 34) in the SIP-010 transfer signature

_SIG_PLACEHOLDER = b"\x00" * 65

# Fixed byte layout of a serialized single-sig spending condition:
# hash_mode(1) + signer(20) + nonce(8) + fee(8) + key_encoding(1) + sig(65).
_SPENDING_CONDITION_LEN = 1 + 20 + 8 + 8 + 1 + 65
# Offset of the origin spending condition inside a serialized tx:
# version(1) + chain_id(4) + auth_type(1).
_ORIGIN_CONDITION_OFFSET = 6


# --- hashing ----------------------------------------------------------------


def _sha512_256(data: bytes) -> bytes:
    return hashlib.new("sha512_256", data).digest()


def _hash160(data: bytes) -> bytes:
    sha = hashlib.sha256(data).digest()
    try:
        return hashlib.new("ripemd160", sha).digest()
    except ValueError:  # OpenSSL built without legacy ripemd160 (common on macOS)
        return _ripemd160_pure(sha)


# Pure-python RIPEMD-160 fallback (public-domain algorithm, RFC-style
# reference implementation) — only used when OpenSSL lacks ripemd160.
def _ripemd160_pure(data: bytes) -> bytes:
    def _rol(x: int, n: int) -> int:
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    _K1 = (0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E)
    _K2 = (0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000)
    _R1 = (
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
        7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
        3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
        1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
        4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13,
    )
    _R2 = (
        5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
        6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
        15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
        8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
        12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11,
    )
    _S1 = (
        11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
        7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
        11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
        11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
        9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6,
    )
    _S2 = (
        8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
        9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
        9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
        15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
        8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11,
    )

    def _f(j: int, x: int, y: int, z: int) -> int:
        if j < 16:
            return x ^ y ^ z
        if j < 32:
            return (x & y) | (~x & z)
        if j < 48:
            return (x | ~y) ^ z
        if j < 64:
            return (x & z) | (y & ~z)
        return x ^ (y | ~z)

    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    msg = bytearray(data)
    bitlen = len(data) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += bitlen.to_bytes(8, "little")

    for off in range(0, len(msg), 64):
        x = [int.from_bytes(msg[off + 4 * i : off + 4 * i + 4], "little") for i in range(16)]
        a1, b1, c1, d1, e1 = h
        a2, b2, c2, d2, e2 = h
        for j in range(80):
            t = (_rol((a1 + _f(j, b1, c1, d1) + x[_R1[j]] + _K1[j // 16]) & 0xFFFFFFFF, _S1[j]) + e1) & 0xFFFFFFFF
            a1, e1, d1, c1, b1 = e1, d1, _rol(c1, 10), b1, t
            t = (_rol((a2 + _f(79 - j, b2, c2, d2) + x[_R2[j]] + _K2[j // 16]) & 0xFFFFFFFF, _S2[j]) + e2) & 0xFFFFFFFF
            a2, e2, d2, c2, b2 = e2, d2, _rol(c2, 10), b2, t
        t = (h[1] + c1 + d2) & 0xFFFFFFFF
        h[1] = (h[2] + d1 + e2) & 0xFFFFFFFF
        h[2] = (h[3] + e1 + a2) & 0xFFFFFFFF
        h[3] = (h[4] + a1 + b2) & 0xFFFFFFFF
        h[4] = (h[0] + b1 + c2) & 0xFFFFFFFF
        h[0] = t
        h = [v & 0xFFFFFFFF for v in h]

    return b"".join(v.to_bytes(4, "little") for v in h)


# --- c32check (Crockford base32 with checksum) ------------------------------

_C32_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_C32_LOOKUP = {c: i for i, c in enumerate(_C32_ALPHABET)}
# c32 normalization: lowercase folded, O→0, L/I→1.
_C32_NORMALIZE = str.maketrans("OLIoli", "011011")


def _c32_encode(data: bytes) -> str:
    """Crockford base32 of the big-endian integer, one leading '0' per
    leading zero byte (canonical c32check form)."""
    num = int.from_bytes(data, "big")
    digits = ""
    while num > 0:
        num, rem = divmod(num, 32)
        digits = _C32_ALPHABET[rem] + digits
    leading_zero_bytes = len(data) - len(data.lstrip(b"\x00"))
    return "0" * leading_zero_bytes + digits


def _c32_decode_str(encoded: str) -> bytes:
    s = encoded.upper().translate(_C32_NORMALIZE)
    if not all(c in _C32_LOOKUP for c in s):
        raise ValueError("invalid c32 string")
    stripped = s.lstrip("0")
    leading_zero_bytes = len(s) - len(stripped)
    num = 0
    for c in stripped:
        num = num * 32 + _C32_LOOKUP[c]
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * leading_zero_bytes + body


def _c32_checksum(version: int, data: bytes) -> bytes:
    payload = bytes([version]) + data
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def c32_address(version: int, hash160: bytes) -> str:
    """Crockford base32 (c32check) encoding — SP/ST addresses."""
    if not 0 <= version < 32:
        raise ValueError("invalid c32 address version")
    if len(hash160) != 20:
        raise ValueError("hash160 must be 20 bytes")
    checksum = _c32_checksum(version, hash160)
    return "S" + _C32_ALPHABET[version] + _c32_encode(hash160 + checksum)


def c32_decode(address: str) -> tuple[int, bytes]:
    if len(address) < 6 or not address.upper().startswith("S"):
        raise ValueError("invalid c32 address")
    norm = address.upper().translate(_C32_NORMALIZE)
    version = _C32_LOOKUP.get(norm[1])
    if version is None:
        raise ValueError("invalid c32 address version")
    payload = _c32_decode_str(norm[2:])
    if len(payload) < 5:
        raise ValueError("invalid c32 address")
    # hash160 is always 20 bytes on Stacks; the bigint decode drops interior
    # sizing, so left-pad/validate to 20 + 4 checksum.
    payload = payload.rjust(24, b"\x00")
    if len(payload) != 24:
        raise ValueError("invalid c32 address")
    data, checksum = payload[:-4], payload[-4:]
    if _c32_checksum(version, data) != checksum:
        raise ValueError("invalid c32 address checksum")
    return version, data


# --- keys -------------------------------------------------------------------


@dataclass(frozen=True)
class StacksKeypair:
    """secp256k1 keypair with the Stacks (c32) address derivations.

    [CHECKLIST #8]: `from_secret` must catch every underlying parse error and
    re-raise ValueError(_INVALID_KEY_MSG) — the secret never appears in any
    exception text or log.
    """

    private_key: bytes  # 32 bytes
    compressed: bool = True  # Stacks convention: 64-hex raw → uncompressed,
    #                          66-hex ending "01" → compressed (stacks.js parity)

    @classmethod
    def from_secret(cls, secret_hex: str) -> "StacksKeypair":
        try:
            s = secret_hex.strip().lower().removeprefix("0x")
            if len(s) == 66:
                if not s.endswith("01"):
                    raise ValueError()
                raw, compressed = s[:64], True
            elif len(s) == 64:
                raw, compressed = s, False
            else:
                raise ValueError()
            key_bytes = bytes.fromhex(raw)
            secret_int = int.from_bytes(key_bytes, "big")
            # 0 < d < n (secp256k1 group order)
            if not 0 < secret_int < _SECP256K1_N:
                raise ValueError()
            return cls(private_key=key_bytes, compressed=compressed)
        except Exception:
            # [CHECKLIST #8] constant message; never chain the original
            # exception (its text can carry key material).
            raise ValueError(_INVALID_KEY_MSG) from None

    def public_key(self) -> bytes:
        """SEC1 pubkey bytes: 33-byte compressed or 65-byte uncompressed
        (matches the keypair's flag; determines address + key-encoding byte)."""
        pub = _eth_keys.PrivateKey(self.private_key).public_key
        raw = pub.to_bytes()  # 64 bytes: x || y
        x, y = raw[:32], raw[32:]
        if self.compressed:
            return bytes([0x02 + (y[-1] & 1)]) + x
        return b"\x04" + raw

    def signer_hash160(self) -> bytes:
        return _hash160(self.public_key())

    def address(self, network: str = "mainnet") -> str:
        """c32check P2PKH address (SP…/ST…)."""
        if network not in _ADDR_VERSION_P2PKH:
            raise ValueError("network must be 'mainnet' or 'testnet'")
        return c32_address(_ADDR_VERSION_P2PKH[network], self.signer_hash160())


# --- serialization helpers --------------------------------------------------


def _clarity_name(name: str) -> bytes:
    encoded = name.encode("ascii")
    if not 0 < len(encoded) <= 128:
        raise ValueError("invalid Clarity name length")
    return bytes([len(encoded)]) + encoded


def _serialize_address(address: str) -> bytes:
    version, h160 = c32_decode(address)
    return bytes([version]) + h160


def _split_contract_id(contract_id: str) -> tuple[str, str]:
    try:
        addr, name = contract_id.split(".")
    except ValueError:
        raise ValueError("contract id must be 'address.contract-name'") from None
    return addr, name


def _cv_uint(value: int) -> bytes:
    if not 0 <= value < 1 << 128:
        raise ValueError("uint out of range")
    return bytes([_CV_UINT]) + value.to_bytes(16, "big")


def _cv_standard_principal(address: str) -> bytes:
    return bytes([_CV_PRINCIPAL_STANDARD]) + _serialize_address(address)


def _cv_buffer(data: bytes) -> bytes:
    return bytes([_CV_BUFFER]) + len(data).to_bytes(4, "big") + data


def _cv_some(inner: bytes) -> bytes:
    return bytes([_CV_OPTIONAL_SOME]) + inner


def _cv_none() -> bytes:
    return bytes([_CV_OPTIONAL_NONE])


# --- post-conditions --------------------------------------------------------


@dataclass(frozen=True)
class PostCondition:
    """Fungible-token post-condition: 'exactly `amount_sats` of sbtc-token
    leave `sender`'. MANDATORY on every transfer this lib builds — it is the
    property that makes handing a signed tx to an untrusted facilitator safe
    (a tampered call aborts on-chain instead of moving more than authorized).
    """

    sender: str          # c32 address
    contract: str        # e.g. SBTC_CONTRACT_TESTNET
    amount_sats: int
    condition: str = "sent_equal_to"

    def serialize(self) -> bytes:
        code = _FT_CONDITION_CODES.get(self.condition)
        if code is None:
            raise ValueError("unknown post-condition code")
        if not 0 <= self.amount_sats < 1 << 64:
            raise ValueError("post-condition amount out of range")
        contract_addr, contract_name = _split_contract_id(self.contract)
        out = bytes([_PC_TYPE_FUNGIBLE])
        out += bytes([_PC_PRINCIPAL_STANDARD]) + _serialize_address(self.sender)
        # Asset info: token contract address + contract name + asset name.
        out += _serialize_address(contract_addr)
        out += _clarity_name(contract_name)
        out += _clarity_name(SBTC_ASSET_NAME)
        out += bytes([code])
        out += self.amount_sats.to_bytes(8, "big")
        return out


# --- transaction build / sign / txid ----------------------------------------


def _serialize_spending_condition(
    signer: bytes,
    nonce: int,
    fee: int,
    key_encoding: int,
    signature: bytes,
) -> bytes:
    if len(signer) != 20 or len(signature) != 65:
        raise ValueError("malformed spending condition")
    return (
        bytes([_HASH_MODE_P2PKH])
        + signer
        + nonce.to_bytes(8, "big")
        + fee.to_bytes(8, "big")
        + bytes([key_encoding])
        + signature
    )


# Sponsor placeholder conditions (SIP-005 sponsored auth, stacks.js parity):
# the SERIALIZED tx carries signer = hash160 of a 33-zero-byte "pubkey"
# (stacks.js `'0'.repeat(66)` placeholder); for the SIGHASH the sponsor
# condition is cleared to an all-zero signer (`newInitialSigHash`). The relay
# replaces the placeholder when it co-signs.
_SPONSOR_PLACEHOLDER = _serialize_spending_condition(
    _hash160(b"\x00" * 33), 0, 0, _KEY_ENCODING_COMPRESSED, _SIG_PLACEHOLDER
)
_SPONSOR_CLEARED = _serialize_spending_condition(
    b"\x00" * 20, 0, 0, _KEY_ENCODING_COMPRESSED, _SIG_PLACEHOLDER
)


def build_sbtc_transfer(
    *,
    sender: StacksKeypair,
    recipient: str,
    amount_sats: int,
    payment_id: str,
    nonce: int,
    fee_microstx: int,
    network: str = "testnet",
    sponsored: bool = False,
) -> bytes:
    """Serialize an UNSIGNED SIP-010 `sbtc-token::transfer` contract call.

    - Clarity args: (amount uint) (sender principal) (recipient principal)
      (memo (optional (buff 34))) — memo = payment_id[:34] utf-8.
      [CHECKLIST #5]: this memo IS the challenge binding; the gateway verifies
      it against the pending payment row. A transfer without it verifies as
      nothing.
    - Post-condition from `amount_sats` is appended automatically (deny-mode).
    - `sponsored=True` selects the sponsored auth variant (relay co-signs and
      pays the STX fee) — wire format only, no relay logic here.
    - Anchor mode: any. Fee/nonce are caller-provided: nonce serialization is
      the SDK's job (client-side sequential — one in-flight signed tx per
      wallet), NOT this lib's.
    """
    if network not in _TX_VERSION:
        raise ValueError("network must be 'mainnet' or 'testnet'")
    if amount_sats <= 0:
        raise ValueError("amount_sats must be positive")
    if not payment_id:
        # [CHECKLIST #5] the memo binding is not optional in this lib.
        raise ValueError("payment_id is required (challenge binding)")
    if nonce < 0 or fee_microstx < 0:
        raise ValueError("nonce and fee must be non-negative")

    memo_bytes = payment_id.encode("utf-8")[:_MEMO_MAX_BYTES]
    contract_id = SBTC_CONTRACT_MAINNET if network == "mainnet" else SBTC_CONTRACT_TESTNET
    contract_addr, contract_name = _split_contract_id(contract_id)
    sender_address = sender.address(network)

    # -- header + auth (unsigned: zeroed signature) --
    out = bytes([_TX_VERSION[network]])
    out += _CHAIN_ID[network].to_bytes(4, "big")
    out += bytes([_AUTH_SPONSORED if sponsored else _AUTH_STANDARD])
    key_encoding = (
        _KEY_ENCODING_COMPRESSED if sender.compressed else _KEY_ENCODING_UNCOMPRESSED
    )
    out += _serialize_spending_condition(
        sender.signer_hash160(), nonce, fee_microstx, key_encoding, _SIG_PLACEHOLDER
    )
    if sponsored:
        out += _SPONSOR_PLACEHOLDER

    # -- anchor mode + post-conditions (deny mode, mandatory) --
    out += bytes([_ANCHOR_MODE_ANY])
    out += bytes([_PC_MODE_DENY])
    pc = PostCondition(
        sender=sender_address, contract=contract_id, amount_sats=amount_sats
    )
    out += (1).to_bytes(4, "big") + pc.serialize()

    # -- payload: contract call --
    out += bytes([_PAYLOAD_CONTRACT_CALL])
    out += _serialize_address(contract_addr)
    out += _clarity_name(contract_name)
    out += _clarity_name("transfer")
    args = [
        _cv_uint(amount_sats),
        _cv_standard_principal(sender_address),
        _cv_standard_principal(recipient),
        _cv_some(_cv_buffer(memo_bytes)),
    ]
    out += len(args).to_bytes(4, "big") + b"".join(args)
    return out


def _origin_condition_fields(tx: bytes) -> tuple[int, int, int]:
    """(nonce, fee, key_encoding) parsed from the origin spending condition."""
    o = _ORIGIN_CONDITION_OFFSET
    if len(tx) < o + _SPENDING_CONDITION_LEN or tx[o] != _HASH_MODE_P2PKH:
        raise ValueError("malformed Stacks transaction")
    nonce = int.from_bytes(tx[o + 21 : o + 29], "big")
    fee = int.from_bytes(tx[o + 29 : o + 37], "big")
    key_encoding = tx[o + 37]
    return nonce, fee, key_encoding


def _with_origin_condition(tx: bytes, condition: bytes) -> bytes:
    o = _ORIGIN_CONDITION_OFFSET
    return tx[:o] + condition + tx[o + _SPENDING_CONDITION_LEN :]


def sign_transaction(unsigned_tx: bytes, keypair: StacksKeypair) -> bytes:
    """Chained presign-sighash signing (SIP-005 §single-sig).

    NOTE FOR CALLERS (the SDK path, AGE-25) — budget semantics around this
    call are checklist territory, enforced in _wallet.py, not here:
      [CHECKLIST #1]  amount bounded by cap BEFORE this is called
      [CHECKLIST #2]  spend recorded when the signed tx LEAVES the process,
                      not at HTTP 200
      [CHECKLIST #3]  once transmitted, the signed tx is live — no other-chain
                      fallback until its fate is known
      [CHECKLIST #7]  validity window clamped before signing
      [CHECKLIST #11] cap math excludes the call's own hold
                      (_cap_excluding_hold is the reference)
      [CHECKLIST #12] absorb+release under ONE lock (_absorb_and_release)
    """
    if len(unsigned_tx) < _ORIGIN_CONDITION_OFFSET + _SPENDING_CONDITION_LEN:
        raise ValueError("malformed Stacks transaction")
    auth_type = unsigned_tx[5]
    if auth_type not in (_AUTH_STANDARD, _AUTH_SPONSORED):
        raise ValueError("malformed Stacks transaction")
    nonce, fee, _ = _origin_condition_fields(unsigned_tx)
    signer = keypair.signer_hash160()
    if unsigned_tx[7:27] != signer:
        raise ValueError("keypair does not match transaction signer")

    # signBegin: the tx serialized with the origin condition CLEARED
    # (nonce=0, fee=0, zero signature). For sponsored auth the sponsor
    # condition is cleared to the all-zero-signer form for the sighash
    # (it serializes differently in the tx itself — see _SPONSOR_PLACEHOLDER).
    cleared = _serialize_spending_condition(
        signer, 0, 0, _KEY_ENCODING_COMPRESSED, _SIG_PLACEHOLDER
    )
    sighash_tx = _with_origin_condition(unsigned_tx, cleared)
    if auth_type == _AUTH_SPONSORED:
        o = _ORIGIN_CONDITION_OFFSET + _SPENDING_CONDITION_LEN
        sighash_tx = (
            sighash_tx[:o] + _SPONSOR_CLEARED + sighash_tx[o + _SPENDING_CONDITION_LEN :]
        )
    sign_begin = _sha512_256(sighash_tx)

    # presign sighash: H(sign_begin || auth_type || fee || nonce).
    # SIP-005 quirk: the ORIGIN always signs with the STANDARD auth-type byte,
    # even in a sponsored transaction — only the sponsor's own signature uses
    # 0x05 (stacks.js `signNextOrigin` hardcodes AuthType.Standard).
    presign = _sha512_256(
        sign_begin
        + bytes([_AUTH_STANDARD])
        + fee.to_bytes(8, "big")
        + nonce.to_bytes(8, "big")
    )

    # RFC6979 deterministic recoverable ECDSA over the presign hash.
    sig = _eth_keys.PrivateKey(keypair.private_key).sign_msg_hash(presign)
    signature = (
        bytes([sig.v]) + sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big")
    )

    key_encoding = (
        _KEY_ENCODING_COMPRESSED if keypair.compressed else _KEY_ENCODING_UNCOMPRESSED
    )
    signed_condition = _serialize_spending_condition(
        signer, nonce, fee, key_encoding, signature
    )
    return _with_origin_condition(unsigned_tx, signed_condition)


def txid_of(signed_tx: bytes) -> str:
    """Deterministic txid (sha512/256 over the signed tx) — computable BEFORE
    broadcast. gateway/stacks.py consumes this for replay protection PRE-settle
    ([CHECKLIST #6] fail-closed), and polls Hiro by it on settle timeout."""
    return _sha512_256(signed_tx).hex()


def sats_from_usd(amount_usd: Decimal, btc_usd_rate: Decimal) -> int:
    """USD→sats for 402 quoting (AGE-24 owns rate sourcing + tolerance;
    this is only the rounding-rule single source of truth: ceil to the sat —
    never quote fewer sats than the USD price)."""
    if btc_usd_rate <= 0:
        raise ValueError("btc_usd_rate must be positive")
    if amount_usd < 0:
        raise ValueError("amount_usd must be non-negative")
    sats = (Decimal(amount_usd) / Decimal(btc_usd_rate) * Decimal(100_000_000)).to_integral_value(
        rounding=ROUND_CEILING
    )
    return int(sats)


def assert_sats_within_cap(amount_sats: int, amount_usd, btc_usd_rate=None) -> None:
    """Refuse to sign a sats amount the USD cap doesn't bound.

    The cap is enforced in USD, but amount_sats is what leaves the wallet.
    Two guards, no I/O: a floor-rate ceiling (STACKS_MIN_BTC_USD, default $10k)
    that holds even without a quoted rate, and a tolerance check against
    btc_usd_rate when present (STACKS_SATS_TOLERANCE, default 2%, min 2 sats).
    Raises ValueError otherwise.
    """
    import os
    if amount_usd is None:
        raise ValueError("no amount_usdc to bound amount_sats against")
    usd = Decimal(str(amount_usd))
    if usd < 0:
        raise ValueError("amount_usd must be non-negative")
    floor = Decimal(os.environ.get("STACKS_MIN_BTC_USD", "10000"))
    max_sats = sats_from_usd(usd, floor)          # cheapest BTC => most sats/$
    if amount_sats > max_sats:
        raise ValueError(
            f"amount_sats={amount_sats} exceeds the most sats ${usd} could buy "
            f"at the ${floor}/BTC floor ({max_sats}) - refusing to sign"
        )
    if btc_usd_rate is not None:
        rate = Decimal(str(btc_usd_rate))
        if rate <= 0:
            raise ValueError(f"402 quotes a non-positive BTC/USD rate ({rate})")
        expected = sats_from_usd(usd, rate)
        tol = Decimal(os.environ.get("STACKS_SATS_TOLERANCE", "0.02"))
        slack = max(Decimal(expected) * tol, Decimal(2))
        if abs(Decimal(amount_sats) - Decimal(expected)) > slack:
            raise ValueError(
                f"amount_sats={amount_sats} inconsistent with ${usd} at {rate} "
                f"BTC/USD (expected ~{expected}) - refusing to sign"
            )
