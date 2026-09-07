#!/usr/bin/env python3
"""Derive a Stacks account key from a Leather/Hiro 24-word Secret Key.

Leather exports a BIP39 mnemonic, not a raw key; the SDK wants the 66-hex
form (`STACKS_AGENT_KEY`). Path: m/44'/5757'/0'/0/<account>, same as
@stacks/wallet-sdk and Leather.

The words are read from a hidden prompt (or stdin when piped) and never
echoed. With --print-key the only thing written to stdout is the key, so:

    export STACKS_AGENT_KEY=$(python tools/stacks_derive_key.py --address SP... --print-key)

Without --print-key it prints addresses only.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import sys
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from agentpay._stacks_tx import StacksKeypair  # noqa: E402

_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_HARDENED = 0x80000000
_STACKS_PATH = (44 | _HARDENED, 5757 | _HARDENED, 0 | _HARDENED, 0)


def mnemonic_to_seed(words: str, passphrase: str = "") -> bytes:
    m = unicodedata.normalize("NFKD", " ".join(words.split()))
    salt = unicodedata.normalize("NFKD", "mnemonic" + passphrase)
    return hashlib.pbkdf2_hmac("sha512", m.encode(), salt.encode(), 2048, 64)


def _ckd_priv(key: bytes, chain: bytes, index: int) -> tuple[bytes, bytes]:
    if index >= _HARDENED:
        data = b"\x00" + key + index.to_bytes(4, "big")
    else:
        data = StacksKeypair(private_key=key, compressed=True).public_key() + index.to_bytes(4, "big")
    digest = hmac.new(chain, data, hashlib.sha512).digest()
    il, ir = digest[:32], digest[32:]
    il_int = int.from_bytes(il, "big")
    child = (il_int + int.from_bytes(key, "big")) % _N
    if child == 0 or il_int >= _N:
        raise ValueError("invalid child key; try the next index")
    return child.to_bytes(32, "big"), ir


def derive_account(seed: bytes, account: int) -> StacksKeypair:
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    for index in (*_STACKS_PATH, account):
        key, chain = _ckd_priv(key, chain, index)
    return StacksKeypair(private_key=key, compressed=True)


def key_hex(kp: StacksKeypair) -> str:
    return kp.private_key.hex() + "01"


def _read_words() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Secret Key (24 words, hidden): ")
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--address", help="stop at the account whose mainnet/testnet address matches")
    p.add_argument("--account", type=int, default=0, help="account index (default 0)")
    p.add_argument("--max-accounts", type=int, default=20, help="how far to search with --address")
    p.add_argument("--passphrase", action="store_true", help="also prompt for a BIP39 passphrase")
    p.add_argument("--print-key", action="store_true", help="write the 66-hex key to stdout (nothing else)")
    a = p.parse_args(argv)

    words = _read_words()
    n_words = len(words.split())
    if n_words not in (12, 15, 18, 21, 24):
        print(f"expected 12/15/18/21/24 words, got {n_words}", file=sys.stderr)
        return 2
    passphrase = getpass.getpass("BIP39 passphrase (hidden, usually empty): ") if a.passphrase else ""
    seed = mnemonic_to_seed(words, passphrase)

    if a.address:
        want = a.address.strip().upper()
        for i in range(a.max_accounts):
            kp = derive_account(seed, i)
            if want in (kp.address("mainnet"), kp.address("testnet")):
                account = i
                break
        else:
            print(f"no account in 0..{a.max_accounts - 1} derives {want}; check the words", file=sys.stderr)
            return 1
    else:
        account = a.account
        kp = derive_account(seed, account)

    if a.print_key:
        sys.stdout.write(key_hex(kp))
        return 0
    print(f"account {account}: {kp.address('mainnet')}  (testnet {kp.address('testnet')})")
    print("rerun with --print-key to emit the key; it is never shown otherwise")
    return 0


if __name__ == "__main__":
    sys.exit(main())
