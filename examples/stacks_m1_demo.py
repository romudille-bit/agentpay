#!/usr/bin/env python3
"""
stacks_m1_demo.py — AGE-26 M1 demo (Stacks sBTC Endowment milestone).

Proves the two M1 acceptance criteria against the LIVE testnet gateway:

  1. a budget-capped Session pays a real sBTC charge on Stacks testnet and
     gets the tool result + a settlement receipt (the on-chain tx);
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

import os
import sys

TESTNET_GATEWAY = "https://gateway-testnet-production.up.railway.app"
TOOL = "token_price"
PARAMS = {"symbol": "BTC"}
EXPLORER = "https://explorer.hiro.so"


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
    from agentpay import Session, PaymentFailed

    print("=" * 68)
    print("1) BUDGET-CAPPED SESSION  ->  sBTC PAYMENT ON STACKS TESTNET")
    print("=" * 68)
    w = _wallet()
    print(f"payer (Stacks testnet): {w.stacks_address}")
    s = Session(wallet=w, gateway_url=TESTNET_GATEWAY, max_spend="0.05",
                prefer_chain="stacks")
    print(f"session cap: ${s.max_spend}   calling {TOOL}({PARAMS}) on the Stacks rail ...\n")
    try:
        r = s.call(TOOL, PARAMS)
        print("  RESULT :", getattr(r, "data", r))
        print("  TX     :", getattr(r, "tx", None))
        print("  NETWORK:", getattr(r, "network", None))
        print("  RECEIPT:", s.spending_summary())
        tx = getattr(r, "tx", None)
        if tx:
            print(f"\n  verify: {EXPLORER}/txid/{tx}?chain=testnet")
    except PaymentFailed as e:
        # A transmitted sBTC tx may still be confirming — surface it, don't hide it.
        print("  settle reported:", str(e)[:220])
        print("  RECEIPT:", s.spending_summary())
        print(f"\n  check the payer's recent txs: {EXPLORER}/address/{w.stacks_address}?chain=testnet")


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
        print(f"  OK  rejected client-side — BudgetExceeded: {str(e)[:160]}")
    print("  RECEIPT:", s.spending_summary(), " (nothing spent)")


if __name__ == "__main__":
    pay_once()
    reject_over_cap()
    print("\nDone. A real Stacks testnet txid above = M1's on-chain payment criterion met.")
