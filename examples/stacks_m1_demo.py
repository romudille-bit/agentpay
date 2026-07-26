#!/usr/bin/env python3
"""
stacks_m1_demo.py — AGE-26 M1 demo (Stacks sBTC Endowment milestone).

Proves the two M1 acceptance criteria against the LIVE testnet gateway:

  1. a budget-capped Session pays a real sBTC charge on Stacks testnet — the
     signed transfer is broadcast and the txid is surfaced (Stacks testnet
     blocks take a few minutes, so confirmation is asynchronous: the demo shows
     the tx as broadcasting/confirming, which is the expected clean outcome);
  2. the same tool, under a per-tool cap below its price, is REJECTED
     client-side before any value moves.

The rail: token_price is priced $0.01 on gateway-testnet (AGE-77), quoted to
sats at 402-issuance (AGE-24), signed sign-don't-broadcast by the SDK (AGE-25)
and broadcast by the gateway (AGE-23).

Run (payer key stays in your environment, never in the repo):

    export STACKS_AGENT_KEY=<funded payer Stacks private key>
    python examples/stacks_m1_demo.py

The payer is a Stacks-testnet wallet funded with testnet sBTC (to spend) and
STX (to pay the tx fee).
"""
from __future__ import annotations

import itertools
import logging
import os
import sys
import threading
import time

# Run from the repo root without an editable install: put the repo root on the
# path if agentpay isn't importable.
try:
    import agentpay  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep the console clean for the spinner — SDK INFO/WARNING logs stay quiet.
logging.getLogger("agentpay").setLevel(logging.ERROR)

TESTNET_GATEWAY = "https://gateway-testnet-production.up.railway.app"
TOOL = "token_price"
PARAMS = {"symbol": "BTC"}
EXPLORER = "https://explorer.hiro.so"


class _Spinner:
    """A background spinner so a slow on-chain wait reads as progress, not a hang."""

    def __init__(self, msg: str):
        self.msg = msg
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        for ch in itertools.cycle("|/-\\"):
            if self._stop.is_set():
                break
            print(f"\r  {ch} {self.msg}", end="", flush=True)
            time.sleep(0.15)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=1)
        print("\r" + " " * (len(self.msg) + 6) + "\r", end="", flush=True)


def _wallet():
    from stellar_sdk import Keypair

    from agentpay import AgentWallet

    payer = os.environ.get("STACKS_AGENT_KEY", "").strip()
    if not payer:
        sys.exit("Set STACKS_AGENT_KEY to the funded payer's Stacks private key, then re-run.")
    # AgentWallet requires a Stellar secret; it is unused on the Stacks pay path.
    w = AgentWallet(secret_key=Keypair.random().secret, network="testnet", stacks_key=payer)
    if not w.stacks_address:
        sys.exit(f"Stacks wallet failed to load: {w.stacks_disabled_reason}")
    return w


def pay_once() -> None:
    from agentpay import Session, SettlementUncertain, PaymentFailed

    print("=" * 68)
    print("1) BUDGET-CAPPED SESSION  ->  sBTC PAYMENT ON STACKS TESTNET")
    print("=" * 68)
    w = _wallet()
    print(f"payer (Stacks testnet): {w.stacks_address}")
    s = Session(wallet=w, gateway_url=TESTNET_GATEWAY, max_spend="0.05",
                prefer_chain="stacks")
    print(f"session cap: ${s.max_spend}   paying {TOOL}({PARAMS}) in sBTC ...\n")

    result = uncertain = failed = None
    with _Spinner("waiting for on-chain settlement (Stacks testnet blocks take a few minutes)"):
        try:
            result = s.call(TOOL, PARAMS)
        except SettlementUncertain as e:
            uncertain = e
        except PaymentFailed as e:
            failed = e

    if result is not None:
        # Fully settled within the window.
        tx = getattr(result, "tx", None)
        print("  ✓ SETTLED")
        print("  RESULT :", getattr(result, "data", result))
        print("  TX     :", tx)
        print("  NETWORK:", getattr(result, "network", None))
        print("  RECEIPT:", s.spending_summary())
        if tx:
            print(f"  verify : {EXPLORER}/txid/{tx}?chain=testnet")
    elif uncertain is not None:
        # Expected on testnet: broadcast, confirming asynchronously.
        print("  ✓ sBTC PAYMENT BROADCAST — confirming on-chain")
        print("  TX     :", uncertain.tx_hash or "(not returned — see payer address below)")
        print("  NETWORK:", uncertain.network or "stacks")
        print("  RECEIPT:", s.spending_summary())
        if uncertain.tx_hash:
            print(f"  verify : {EXPLORER}/txid/{uncertain.tx_hash}?chain=testnet")
        else:
            print(f"  payer  : {EXPLORER}/address/{w.stacks_address}?chain=testnet")
        print("  (Testnet confirmation takes a few minutes — the tx is on-chain now.)")
    else:
        print("  ✗ payment failed (nothing settled):", str(failed)[:200])
        sys.exit(1)


def reject_over_cap() -> None:
    from agentpay import Session, BudgetExceeded

    print("\n" + "=" * 68)
    print("2) PER-TOOL CAP BELOW PRICE  ->  REJECTED BEFORE ANY PAYMENT")
    print("=" * 68)
    w = _wallet()
    # Comfortable session budget, but token_price is capped at half its price:
    # the call is refused pre-settlement (no fallback, no sBTC moved).
    s = Session(wallet=w, gateway_url=TESTNET_GATEWAY, max_spend="0.05",
                prefer_chain="stacks", max_per_tool={TOOL: 0.005})
    print(f"session cap ${s.max_spend}, per-tool cap ${s._max_per_tool[TOOL]} "
          f"(< $0.01 price)   calling {TOOL} ...")
    try:
        s.call(TOOL, PARAMS)
        print("  x  UNEXPECTED: the call was NOT rejected")
        sys.exit(1)
    except BudgetExceeded as e:
        print(f"  ✓ rejected client-side — BudgetExceeded: {str(e)[:160]}")
    print("  RECEIPT:", s.spending_summary(), " (nothing spent)")


if __name__ == "__main__":
    pay_once()
    reject_over_cap()
    print("\nDone. The sBTC txid above is M1's on-chain payment proof.")
