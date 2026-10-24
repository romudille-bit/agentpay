#!/usr/bin/env python3
"""Send USDC on Base mainnet from a key held in .env — the key is never printed.

    python3 tools/send_usdc_base.py --to 0xRECIPIENT --amount 1.00
    python3 tools/send_usdc_base.py --to 0x… --amount 1.00 --key-env AGENT_BASE_KEY_TEST --dry-run

A plain ERC-20 transfer needs ETH for gas (unlike the gateway's EIP-3009 settles,
where the facilitator pays). The script checks both balances and refuses early.
"""
import argparse, json, os, sys, time, urllib.request
from pathlib import Path

from eth_account import Account
from eth_utils import is_address, to_checksum_address

RPC      = "https://mainnet.base.org"
CHAIN_ID = 8453
USDC     = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GAS_LIMIT = 80_000
BASESCAN = "https://basescan.org/tx/"


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={
        "content-type": "application/json", "user-agent": "agentpay-tools/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if "error" in out:
        raise SystemExit(f"RPC {method}: {out['error']}")
    return out["result"]


def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update(os.environ)
    return env


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="recipient 0x address on Base")
    ap.add_argument("--amount", required=True, type=float, help="USDC amount, e.g. 1.00")
    ap.add_argument("--key-env", default="AGENT_BASE_KEY_TEST",
                    help="name of the .env variable holding the private key")
    ap.add_argument("--dry-run", action="store_true", help="check and build, do not broadcast")
    a = ap.parse_args()

    if not is_address(a.to):
        raise SystemExit(f"not an EVM address: {a.to}")
    to = to_checksum_address(a.to)
    if to.lower() == USDC.lower():
        raise SystemExit("refusing to send to the USDC contract itself")

    env = load_env(Path(__file__).resolve().parents[1] / ".env")
    key = env.get(a.key_env)
    if not key:
        raise SystemExit(f"{a.key_env} not found in .env or environment")
    acct = Account.from_key(key)
    sender = acct.address

    amount_atomic = int(round(a.amount * 1_000_000))
    if amount_atomic <= 0:
        raise SystemExit("amount must be > 0")

    bal_data = "0x70a08231" + sender[2:].lower().rjust(64, "0")
    usdc_bal = int(rpc("eth_call", [{"to": USDC, "data": bal_data}, "latest"]), 16)
    eth_bal  = int(rpc("eth_getBalance", [sender, "latest"]), 16)
    gas_price = int(rpc("eth_gasPrice", []), 16)
    gas_cost  = GAS_LIMIT * gas_price

    print(f"from    {sender}")
    print(f"to      {to}")
    print(f"amount  {amount_atomic / 1e6:.6f} USDC   (balance {usdc_bal / 1e6:.6f})")
    print(f"gas     ≤ {gas_cost / 1e18:.9f} ETH       (balance {eth_bal / 1e18:.9f})")
    if usdc_bal < amount_atomic:
        raise SystemExit("insufficient USDC")
    if eth_bal < gas_cost:
        raise SystemExit("insufficient ETH for gas — this is the 'not enough funds' the wallet apps show")

    calldata = ("0xa9059cbb" + to[2:].lower().rjust(64, "0")
                + hex(amount_atomic)[2:].rjust(64, "0"))
    nonce = int(rpc("eth_getTransactionCount", [sender, "pending"]), 16)
    tx = {
        "chainId": CHAIN_ID, "nonce": nonce, "to": USDC, "value": 0, "data": calldata,
        "gas": GAS_LIMIT, "maxFeePerGas": gas_price * 2, "maxPriorityFeePerGas": gas_price,
    }
    signed = acct.sign_transaction(tx)
    if a.dry_run:
        print("dry run — transaction built and signed, not broadcast")
        return
    if input("broadcast? [y/N] ").strip().lower() != "y":
        print("aborted")
        return

    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction   # eth-account <0.13
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")])
    print(f"sent    {BASESCAN}{tx_hash}")
    for _ in range(60):
        rcpt = rpc("eth_getTransactionReceipt", [tx_hash])
        if rcpt:
            ok = int(rcpt["status"], 16) == 1
            print("status ", "confirmed" if ok else "REVERTED", f"(block {int(rcpt['blockNumber'], 16)})")
            sys.exit(0 if ok else 1)
        time.sleep(2)
    print("still pending — check the Basescan link")


if __name__ == "__main__":
    main()
