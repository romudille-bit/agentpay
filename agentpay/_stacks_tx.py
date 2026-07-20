"""
_stacks_tx.py — Minimal Python Stacks transaction signing (AGE-22).

STATUS: SKELETON (2026-07-20). Nothing imports this module yet — safe to ship
inert. Design doc: docs/stacks-adapter.md (read the 12-point checklist first).

The long pole of the Stacks adapter: no mature Python Stacks signing library exists
(canonical tooling is stacks.js), so this is a minimal, spec-documented
implementation of exactly what the x402 payment path needs — a signed SIP-010
`sbtc-token::transfer` contract call — and nothing else.

Specs: SIP-005 (transaction encoding), SIP-010 (fungible token trait),
Hiro API (`POST /v2/transactions`, `GET /extended/v1/tx/{txid}`).
Curve: secp256k1 — same as EVM; primitives already in the dependency tree.

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

from dataclasses import dataclass
from decimal import Decimal

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

_INVALID_KEY_MSG = "invalid Stacks private key"  # [CHECKLIST #8] constant, never interpolate


@dataclass(frozen=True)
class StacksKeypair:
    """secp256k1 keypair with the Stacks (c32) address derivations.

    [CHECKLIST #8]: `from_secret` must catch every underlying parse error and
    re-raise ValueError(_INVALID_KEY_MSG) — the secret never appears in any
    exception text or log.
    """

    private_key: bytes  # 32 bytes (+ compressed flag handled separately)

    @classmethod
    def from_secret(cls, secret_hex: str) -> "StacksKeypair":
        raise NotImplementedError("AGE-22")

    def address(self, network: str = "mainnet") -> str:
        """c32check P2PKH address (SP…/ST…)."""
        raise NotImplementedError("AGE-22")


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


def c32_address(version: int, hash160: bytes) -> str:
    """Crockford base32 (c32check) encoding — SP/ST addresses."""
    raise NotImplementedError("AGE-22")


def c32_decode(address: str) -> tuple[int, bytes]:
    raise NotImplementedError("AGE-22")


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
    raise NotImplementedError("AGE-22")


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
    raise NotImplementedError("AGE-22")


def txid_of(signed_tx: bytes) -> str:
    """Deterministic txid (sha512/256 over the signed tx) — computable BEFORE
    broadcast. gateway/stacks.py consumes this for replay protection PRE-settle
    ([CHECKLIST #6] fail-closed), and polls Hiro by it on settle timeout."""
    raise NotImplementedError("AGE-22")


def sats_from_usd(amount_usd: Decimal, btc_usd_rate: Decimal) -> int:
    """USD→sats for 402 quoting (AGE-24 owns rate sourcing + tolerance;
    this is only the rounding-rule single source of truth: ceil to the sat —
    never quote fewer sats than the USD price)."""
    raise NotImplementedError("AGE-24")
