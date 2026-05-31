#!/usr/bin/env python3
"""
set_wallet.py — securely write a wallet into the local .env
────────────────────────────────────────────────────────────
You type the public address openly, then the secret key is read
*hidden* (never echoed, never stored in shell history). Both are
written to the gitignored .env at the repo root, which is then
locked to owner-only permissions (chmod 600).

Run:
  python3 tools/set_wallet.py
"""

import getpass
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH  = os.path.join(REPO_ROOT, ".env")

# Which wallet are we setting? Maps a friendly choice to the .env var names.
WALLETS = {
    "1": {
        "name":      "Testing wallet (Base mainnet, disposable)",
        "addr_var":  "AGENT_BASE_ADDRESS_TEST",
        "secret_var": "AGENT_BASE_KEY_TEST",
        "network":   "base",
    },
    "2": {
        "name":      "Gateway wallet (Stellar)",
        "addr_var":  "GATEWAY_PUBLIC_KEY",
        "secret_var": "GATEWAY_SECRET_KEY",
        "network":   "stellar",
    },
    "3": {
        "name":      "Testing wallet (Stellar mainnet, funded + trustline)",
        "addr_var":  "AGENT_STELLAR_ADDRESS_TEST",
        "secret_var": "AGENT_STELLAR_KEY_TEST",
        "network":   "stellar",
    },
    # ── No Base gateway SECRET today ──────────────────────────────────────────
    # The gateway only RECEIVES on Base (BASE_GATEWAY_ADDRESS) and verifies txs
    # by reading the chain — it never signs a Base transaction, so there is no
    # Base gateway private key to store here. If Base payouts are ever added
    # (refunds / 85% split on Base = "outgoing Base tx machinery"), the gateway
    # would then need a Base signing key. Uncomment and fill in at that point:
    #
    # "3": {
    #     "name":       "Gateway wallet (Base mainnet) — payouts",
    #     "addr_var":   "BASE_GATEWAY_ADDRESS",
    #     "secret_var": "GATEWAY_BASE_KEY",
    #     "network":    "base",
    # },
}


def validate_address(network: str, value: str) -> str | None:
    """Return an error string if the address looks wrong, else None."""
    if network == "base":
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
            return "Base address must be 0x + 40 hex chars."
    elif network == "stellar":
        if not re.fullmatch(r"G[A-Z2-7]{55}", value):
            return "Stellar address must start with G and be 56 chars."
    return None


def validate_secret(network: str, value: str) -> str | None:
    if network == "base":
        v = value.lower().removeprefix("0x")
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            return "Base private key must be 64 hex chars (optionally 0x-prefixed)."
    elif network == "stellar":
        if not re.fullmatch(r"S[A-Z2-7]{55}", value):
            return "Stellar secret must start with S and be 56 chars."
    return None


def upsert_env(path: str, updates: dict[str, str]) -> None:
    """Insert or replace each KEY=value line in the .env, preserving the rest."""
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r") as fh:
            lines = fh.read().splitlines()

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    # Append any keys that weren't already present.
    for key, val in remaining.items():
        out.append(f"{key}={val}")

    # Write with restrictive perms from the start.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.chmod(path, 0o600)


def main() -> None:
    print()
    print("  Which wallet are you setting?")
    for k, w in WALLETS.items():
        print(f"    [{k}] {w['name']}")
    choice = input("  > ").strip()
    wallet = WALLETS.get(choice)
    if not wallet:
        print("  Unknown choice. Aborting.")
        sys.exit(1)

    print()
    print(f"  Setting: {wallet['name']}")
    print()

    # 1) Public address — typed openly (it is not a secret).
    address = input(f"  Public address ({wallet['addr_var']}): ").strip()
    err = validate_address(wallet["network"], address)
    if err:
        print(f"  ✗ {err}")
        sys.exit(1)

    # 2) Secret — read HIDDEN, never echoed, never in shell history.
    secret = getpass.getpass(f"  Secret key  ({wallet['secret_var']}, hidden): ").strip()
    err = validate_secret(wallet["network"], secret)
    if err:
        print(f"  ✗ {err}")
        sys.exit(1)

    # Normalize Base keys to a consistent 0x-prefixed form.
    if wallet["network"] == "base" and not secret.lower().startswith("0x"):
        secret = "0x" + secret

    confirm = input(f"\n  Write these to {ENV_PATH}? [y/N] ").strip().lower()
    if confirm != "y":
        print("  Aborted. Nothing written.")
        sys.exit(0)

    upsert_env(ENV_PATH, {
        wallet["addr_var"]:   address,
        wallet["secret_var"]: secret,
    })

    print()
    print(f"  ✓ Saved {wallet['addr_var']} and {wallet['secret_var']} to .env")
    print(f"  ✓ .env permissions locked to owner-only (600)")
    print(f"  Address: {address}")
    print(f"  Secret:  (hidden — never displayed)")
    print()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  Aborted.")
        sys.exit(1)
