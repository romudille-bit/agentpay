#!/usr/bin/env python3
"""AGE-111: settle one paid call per listing so Bazaar re-indexes the new
serviceName. Bazaar reads the resource off the live 402 at settle time, so a
deploy alone changes nothing. $0.01 each, capped at $0.05.

    ./venv/bin/python tools/reindex_bazaar.py            # dry run
    ./venv/bin/python tools/reindex_bazaar.py --confirm  # shows wallet, asks y/N

Don't commit a key pasted into PASTE_KEY_HERE — tools/ is tracked.
"""

import argparse
import getpass
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASTE_KEY_HERE = ""

MAX_SPEND = "0.05"
GATEWAY = os.environ.get("AGENTPAY_GATEWAY", "https://agentpay.tools")
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")

KNOWN = {
    "0xe1601c10b8d4dbf71e0c592b779520380174bc3a":
        "flagship analyst — disclosed paying customer (RECOMMENDED)",
    "0xe8b25a72dd6aef69515452a61ad231c7df2843b7":
        "GATEWAY wallet — would pay itself in a circle; use the flagship",
}

PLAN = [
    ("session_create",  {"max_spend": "0.10"}, "idle since 2026-07-15"),
    ("pre_trade_check", {"symbol": "ETH", "side": "buy", "size_usd": 100},
     "last paid 2026-08-05"),
    ("verified_route",  {"need": "dex pair liquidity", "budget_usd": 0.01},
     "last paid 2026-08-06"),
]


def usdc_balance(addr):
    data = "0x70a08231" + addr[2:].lower().rjust(64, "0")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": USDC_BASE, "data": data}, "latest"]})
    try:
        req = urllib.request.Request(
            BASE_RPC, data=body.encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
            res = json.loads(r.read().decode()).get("result")
        return int(res, 16) / 1_000_000 if res else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--only", metavar="TOOL")
    args = ap.parse_args()

    plan = [p for p in PLAN if not args.only or p[0] == args.only]
    if not plan:
        sys.exit(f"no such tool in plan: {args.only}")

    print(f"gateway   : {GATEWAY}")
    print(f"cap       : ${MAX_SPEND}")
    print(f"est. cost : ${0.01 * len(plan):.2f}  ({len(plan)} call(s))\n")
    for name, params, why in plan:
        print(f"  {name:<17} {params}  ({why})")

    if not args.confirm:
        print("\nDRY RUN — nothing spent.")
        return

    key = (PASTE_KEY_HERE.strip() or os.environ.get("AGENTPAY_BASE_KEY", "").strip()
           or getpass.getpass("\nBase private key (hidden, not stored): ").strip())
    if not key:
        sys.exit("no key given — nothing spent.")
    if not key.startswith("0x"):
        key = "0x" + key

    try:
        from eth_account import Account
        addr = Account.from_key(key).address
    except Exception as e:
        sys.exit(f"bad key: {e}")

    bal = usdc_balance(addr)
    print(f"\n  wallet  : {addr[:6]}…{addr[-4:]}   (full: {addr})")
    print(f"  identity: {KNOWN.get(addr.lower(), 'unknown wallet')}")
    print(f"  USDC    : {bal if bal is not None else 'could not read (RPC)'}")
    if bal is not None and bal < 0.01 * len(plan):
        sys.exit("insufficient USDC — nothing spent.")

    if input(f"\nSpend ${0.01 * len(plan):.2f} from {addr[:6]}…{addr[-4:]}? "
             "type 'yes': ").strip().lower() != "yes":
        sys.exit("aborted — nothing spent.")

    from agentpay import AgentWallet, Session
    print("\nsettling...\n")
    with Session(AgentWallet(base_key=key), gateway_url=GATEWAY,
                 max_spend=MAX_SPEND) as s:
        for name, params, _ in plan:
            try:
                r = s.call(name, params)
                rec = (r or {}).get("receipt") or {}
                tx = rec.get("tx_hash") or rec.get("transaction_hash") or "?"
                print(f"  ✓ {name:<17} tx={tx}")
                if tx != "?":
                    print(f"      https://basescan.org/tx/{tx}")
            except Exception as e:
                print(f"  ✗ {name:<17} {type(e).__name__}: {e}")
        try:
            print(f"\nspent: {s.spent()}   remaining: {s.remaining()}")
        except Exception:
            pass

    print("\nWait a few minutes, then: ./venv/bin/python tools/bazaar_visibility.py")


if __name__ == "__main__":
    main()
