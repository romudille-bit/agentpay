"""Rotate the gateway's Stellar signing key without changing its address.

One tx signed by the current key: add a fresh signer (weight 1), set the
master key to weight 0, thresholds 1/1/1. pay_to, developer_address rows
and listings stay the same; only GATEWAY_SECRET_KEY changes.

    python tools/rotate_stellar_signer.py --network mainnet --dry-run
    python tools/rotate_stellar_signer.py --network mainnet

Run locally. Reads GATEWAY_SECRET_KEY from env or prompts; prints the new
secret once, writes nothing to disk. --dry-run only inspects the account and
describes the ops; nothing is signed, so nothing printed can be submitted.
Needs the gateway to load its account from settings.GATEWAY_PUBLIC_KEY
(stellar.py / faucet.py), not from the secret.
"""
import argparse
import getpass
import os
import sys

from stellar_sdk import Keypair, Network, Server, Signer, TransactionBuilder

HORIZON = {
    "mainnet": "https://horizon.stellar.org",
    "testnet": "https://horizon-testnet.stellar.org",
}
PASSPHRASE = {
    "mainnet": Network.PUBLIC_NETWORK_PASSPHRASE,
    "testnet": Network.TESTNET_NETWORK_PASSPHRASE,
}


def describe(server: Server, account_id: str) -> dict:
    raw = server.accounts().account_id(account_id).call()
    return {
        "signers": [(s["key"], s["weight"]) for s in raw["signers"]],
        "thresholds": raw["thresholds"],
        "xlm": next(b["balance"] for b in raw["balances"] if b["asset_type"] == "native"),
        "sequence": raw["sequence"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", choices=list(HORIZON), required=True)
    ap.add_argument("--dry-run", action="store_true", help="inspect the account and describe the ops; sign nothing")
    args = ap.parse_args()

    old_secret = os.environ.get("GATEWAY_SECRET_KEY") or getpass.getpass(
        f"Current GATEWAY_SECRET_KEY ({args.network}): "
    )
    old = Keypair.from_secret(old_secret.strip())
    server = Server(HORIZON[args.network])

    try:
        before = describe(server, old.public_key)
    except Exception as e:
        print(f"\nERROR: could not load {old.public_key} on {args.network}: {e}\n"
              "Wrong secret for this network?", file=sys.stderr)
        return 2
    print(f"\nAccount   : {old.public_key}")
    print(f"Network   : {args.network}")
    print(f"XLM       : {before['xlm']}")
    print(f"Signers   : {before['signers']}")
    print(f"Thresholds: {before['thresholds']}")

    master_weight = next((w for k, w in before["signers"] if k == old.public_key), 0)
    if master_weight == 0:
        print("\nERROR: the master key already has weight 0 — this secret cannot sign. "
              "If you did not do this, the account may already be taken over.", file=sys.stderr)
        return 2
    if len(before["signers"]) > 1:
        print("\nWARNING: extra signers already present. Check them before proceeding.", file=sys.stderr)
        if input("Continue anyway? [y/N] ").strip().lower() != "y":
            return 1

    if args.dry_run:
        print("\n[dry-run] would submit one tx from this account: add a fresh ed25519 "
              "signer (weight 1), set master weight 0, thresholds 1/1/1. Nothing signed.")
        return 0

    new = Keypair.random()

    # Show the new secret BEFORE building or submitting. Once the tx lands the
    # old key is dead; losing the new secret would lock the account forever.
    print(f"\nNew signer: {new.public_key}")
    print("\n" + "=" * 72)
    print("SAVE THIS NOW (password manager), then confirm below:")
    print(f"GATEWAY_SECRET_KEY (NEW)       : {new.secret}")
    print("=" * 72)
    if input("Type the last 4 characters of the new secret to confirm it is saved: ").strip() != new.secret[-4:]:
        print("Mismatch — nothing submitted. Old key still active.", file=sys.stderr)
        return 1

    # Build after the confirm so the time bound starts now, not during it.
    account = server.load_account(old.public_key)
    tx = (
        TransactionBuilder(
            source_account=account,
            network_passphrase=PASSPHRASE[args.network],
            base_fee=200,
        )
        .append_set_options_op(signer=Signer.ed25519_public_key(new.public_key, 1))
        .append_set_options_op(master_weight=0, low_threshold=1, med_threshold=1, high_threshold=1)
        .set_timeout(300)
        .build()
    )
    tx.sign(old)

    print("\nSubmitting...")
    resp = server.submit_transaction(tx)
    print(f"Tx hash   : {resp['hash']}")

    after = describe(server, old.public_key)
    ok = (
        (new.public_key, 1) in after["signers"]
        and not any(k == old.public_key and w > 0 for k, w in after["signers"])
    )
    print(f"Signers   : {after['signers']}")
    print(f"Thresholds: {after['thresholds']}")
    if not ok:
        print("\nERROR: post-rotation state unexpected — inspect the account on stellar.expert.", file=sys.stderr)
        return 3

    print("\n" + "=" * 72)
    print("ROTATION DONE. The old secret is now dead for this account.")
    print(f"GATEWAY_PUBLIC_KEY (unchanged) : {old.public_key}")
    print(f"GATEWAY_SECRET_KEY (NEW)       : {new.secret}")
    print("=" * 72)
    print("Next: paste the new secret into Railway (service: gateway for mainnet, "
          "gateway-testnet for testnet) and into .env. Redeploy when builds resume.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
