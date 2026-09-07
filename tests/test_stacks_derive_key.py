"""tools/stacks_derive_key.py against @stacks/wallet-sdk generateWallet vectors."""

import importlib.util
import io
import pathlib
import sys

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "tools" / "stacks_derive_key.py"
_spec = importlib.util.spec_from_file_location("stacks_derive_key", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

MNEMONIC = " ".join(["abandon"] * 23 + ["art"])

# (account, 66-hex key, mainnet address) from @stacks/wallet-sdk 7.x
VECTORS = [
    (0, "2f9f6a682e218463526a145535b6e3c8d2a12885fb658fbb318bc8fc0c8d400501", "SP1JAHE8GEHB0MCBGR8J6W0AA7TJEE1XKFTFJMQ5W"),
    (1, "5c6309c86438485e2c9e44fbf3c4c32fecd9b97f962be34feb0371a0e14fa0df01", "SPHPEJV3VWTXCYV9K5FE95VXNGVEGBKGJYHB8F5K"),
    (2, "96372ad8a2e2c1acd16f3d0c26f81cea53e7f81c4b6edebf3e271c37b6dee34f01", "SP35Q8MXCYVDPZE7RXG4TFKBTFQRSTYC9C63R4TTR"),
]


@pytest.mark.parametrize("account,key,address", VECTORS)
def test_matches_wallet_sdk(account, key, address):
    kp = mod.derive_account(mod.mnemonic_to_seed(MNEMONIC), account)
    assert mod.key_hex(kp) == key
    assert kp.address("mainnet") == address


def test_seed_is_bip39():
    # BIP39 reference vector for this mnemonic with passphrase "TREZOR"
    seed = mod.mnemonic_to_seed(MNEMONIC, "TREZOR")
    assert seed.hex().startswith("bda85446c68413707090a52022edd26a")


def test_extra_whitespace_and_case_do_not_matter():
    messy = "  " + MNEMONIC.replace(" ", "   ") + "\n"
    assert mod.mnemonic_to_seed(messy) == mod.mnemonic_to_seed(MNEMONIC)


def test_cli_finds_account_by_address(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(MNEMONIC))
    rc = mod.main(["--address", VECTORS[1][2], "--print-key"])
    assert rc == 0
    assert capsys.readouterr().out == VECTORS[1][1]


def test_cli_without_print_key_never_prints_the_key(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(MNEMONIC))
    assert mod.main(["--account", "0"]) == 0
    out = capsys.readouterr().out
    assert VECTORS[0][2] in out
    assert VECTORS[0][1][:16] not in out


def test_cli_unknown_address_fails(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(MNEMONIC))
    assert mod.main(["--address", "SP000", "--max-accounts", "3"]) == 1
    assert capsys.readouterr().out == ""


def test_cli_rejects_bad_word_count(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("one two three"))
    assert mod.main([]) == 2
