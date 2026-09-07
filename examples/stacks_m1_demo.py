#!/usr/bin/env python3
"""
stacks_m1_demo.py — Stacks sBTC demo (Endowment milestones M1 / M2).

Proves the two acceptance criteria against a LIVE gateway — testnet by
default, mainnet with STACKS_NETWORK=mainnet:

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
    python examples/stacks_m1_demo.py                     # testnet, token_price
    STACKS_NETWORK=mainnet python examples/stacks_m1_demo.py   # mainnet, pre_trade_check

The payer wallet holds sBTC (to spend) and a little STX (the tx fee) on the
chosen network. Override the gateway with AGENTPAY_GATEWAY_URL and the tool
with AGENTPAY_DEMO_TOOL. If the gateway cannot confirm the settle inside its
window the SDK raises SettlementUncertain; the demo then redeems: it waits
for the tx to confirm and re-presents the same signed payment. If that redeem
was interrupted, STACKS_REDEEM_TXID=<txid> redeems it from the chain alone.
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

NETWORK = os.environ.get("STACKS_NETWORK", "testnet").strip().lower() or "testnet"
if NETWORK not in ("testnet", "mainnet"):
    sys.exit("STACKS_NETWORK must be 'testnet' or 'mainnet'")
GATEWAYS = {
    "testnet": "https://gateway-testnet-production.up.railway.app",
    "mainnet": "https://agentpay.tools",
}
GATEWAY = os.environ.get("AGENTPAY_GATEWAY_URL", "").rstrip("/") or GATEWAYS[NETWORK]
TOOL = os.environ.get("AGENTPAY_DEMO_TOOL") or ("token_price" if NETWORK == "testnet" else "pre_trade_check")
PARAMS = {"symbol": "BTC"}
EXPLORER = "https://explorer.hiro.so"
NET = NETWORK.upper()


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
    from agentpay import AgentWallet

    payer = os.environ.get("STACKS_AGENT_KEY", "").strip()
    if not payer:
        sys.exit("Set STACKS_AGENT_KEY to the funded payer's Stacks private key, then re-run.")
    # Stacks-only payer: no Stellar secret needed.
    w = AgentWallet(network=NETWORK, stacks_key=payer)
    if not w.stacks_address:
        sys.exit(f"Stacks wallet failed to load: {w.stacks_disabled_reason}")
    return w


def pay_once() -> None:
    from agentpay import Session, SettlementUncertain, PaymentFailed

    print("=" * 68)
    print(f"1) BUDGET-CAPPED SESSION  ->  sBTC PAYMENT ON STACKS {NET}")
    print("=" * 68)
    w = _wallet()
    print(f"payer (Stacks {NETWORK}): {w.stacks_address}   gateway: {GATEWAY}")
    s = Session(wallet=w, gateway_url=GATEWAY, max_spend="0.05",
                prefer_chain="stacks")
    print(f"session cap: ${s.max_spend}   paying {TOOL}({PARAMS}) in sBTC ...\n")

    result = uncertain = failed = None
    with _Spinner("waiting for on-chain settlement"):
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
            print(f"  verify : {EXPLORER}/txid/0x{str(tx).removeprefix('0x')}?chain={NETWORK}")
    elif uncertain is not None:
        # Broadcast, confirming asynchronously: redeem once the tx confirms.
        print("  ✓ sBTC PAYMENT BROADCAST — confirming on-chain")
        print("  TX     :", uncertain.tx_hash or "(not returned — see payer address below)")
        if uncertain.tx_hash:
            print(f"  verify : {EXPLORER}/txid/0x{uncertain.tx_hash}?chain={NETWORK}")
        else:
            print(f"  payer  : {EXPLORER}/address/{w.stacks_address}?chain={NETWORK}")
        redeemed = None
        with _Spinner("waiting for confirmation, then redeeming the same signed payment"):
            try:
                redeemed = s.redeem(uncertain, wait_s=600, poll_s=10)
            except SettlementUncertain as e:
                print("\n  still unconfirmed:", str(e)[:160])
            except PaymentFailed as e:
                print("\n  ✗ redeem refused:", str(e)[:200])
                sys.exit(1)
        if redeemed is not None:
            print("  ✓ REDEEMED — tool result delivered for the confirmed payment")
            print("  RESULT :", redeemed.get("result"))
        print("  RECEIPT:", s.spending_summary())
    else:
        print("  ✗ payment failed (nothing settled):", str(failed)[:200])
        sys.exit(1)


def redeem_only(txid: str) -> None:
    from agentpay import Session, SettlementUncertain, PaymentFailed

    print("=" * 68)
    print(f"REDEEM FROM CHAIN  ->  {txid[:16]}… on STACKS {NET}")
    print("=" * 68)
    s = Session(wallet=_wallet(), gateway_url=GATEWAY, max_spend="0.05",
                prefer_chain="stacks")
    try:
        redeemed = s.redeem_txid(txid, TOOL, PARAMS, wait_s=600, poll_s=10)
    except SettlementUncertain as e:
        sys.exit(f"  still unconfirmed: {str(e)[:160]}")
    except PaymentFailed as e:
        sys.exit(f"  ✗ redeem refused: {str(e)[:200]}")
    print("  ✓ REDEEMED — tool result delivered for the confirmed payment")
    print("  RESULT :", redeemed.get("result"))
    print(f"  verify : {EXPLORER}/txid/0x{txid.removeprefix('0x')}?chain={NETWORK}")


def reject_over_cap() -> None:
    from agentpay import Session, BudgetExceeded

    print("\n" + "=" * 68)
    print("2) PER-TOOL CAP BELOW PRICE  ->  REJECTED BEFORE ANY PAYMENT")
    print("=" * 68)
    w = _wallet()
    # Comfortable session budget, but the tool is capped at half its price:
    # the call is refused pre-settlement (no fallback, no sBTC moved).
    per_tool_cap = 0.005   # below the tool's $0.01 -> refused pre-settlement
    s = Session(wallet=w, gateway_url=GATEWAY, max_spend="0.05",
                prefer_chain="stacks", max_per_tool={TOOL: per_tool_cap})
    print(f"session cap ${s.max_spend}, per-tool cap ${per_tool_cap} "
          f"(< $0.01 price)   calling {TOOL} ...")
    try:
        s.call(TOOL, PARAMS)
        print("  x  UNEXPECTED: the call was NOT rejected")
        sys.exit(1)
    except BudgetExceeded as e:
        print(f"  ✓ rejected client-side — BudgetExceeded: {str(e)[:160]}")
    print("  RECEIPT:", s.spending_summary(), " (nothing spent)")


if __name__ == "__main__":
    if os.environ.get("STACKS_REDEEM_TXID"):
        redeem_only(os.environ["STACKS_REDEEM_TXID"].strip())
        sys.exit(0)
    pay_once()
    reject_over_cap()
    print(f"\nDone. The sBTC txid above is the on-chain payment proof (Stacks {NETWORK}).")
