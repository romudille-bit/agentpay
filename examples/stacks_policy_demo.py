#!/usr/bin/env python3
"""
stacks_policy_demo.py — spending rules beyond the cap, on the Stacks rail.

Runs four scenarios against a live gateway (testnet by default, mainnet with
STACKS_NETWORK=mainnet) with the same funded payer:

  1. recipient allowlist — the gateway's payee is not on the list: the 402
     is received, nothing is signed, the call is refused;
  2. per-call maximum — the tool's price is over the rule: refused on the
     quote, before any request;
  3. approval gate — the price is above the threshold and the approver says
     no: 402 received, nothing signed; then the approver says yes and the
     call settles in sBTC (the one paid call of this demo);
  4. the receipt — every refusal appears under `anomalies`, the settled leg
     under `breakdown`.

    export STACKS_AGENT_KEY=<funded payer Stacks private key>
    python examples/stacks_policy_demo.py
    STACKS_NETWORK=mainnet python examples/stacks_policy_demo.py

Override the gateway with AGENTPAY_GATEWAY_URL and the tool with
AGENTPAY_DEMO_TOOL (a $0.01 tool is assumed for the thresholds below).
"""
from __future__ import annotations

import logging
import os
import sys

try:
    import agentpay  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
# A Stacks address that is not the gateway's payee, for the allowlist scenario.
NOT_THE_PAYEE = "SP000000000000000000002Q6VF78"


def _wallet():
    from agentpay import AgentWallet
    payer = os.environ.get("STACKS_AGENT_KEY", "").strip()
    if not payer:
        sys.exit("Set STACKS_AGENT_KEY to the funded payer's Stacks private key, then re-run.")
    w = AgentWallet(network=NETWORK, stacks_key=payer)
    if not w.stacks_address:
        sys.exit(f"Stacks wallet failed to load: {w.stacks_disabled_reason}")
    return w


def _banner(n: int, title: str) -> None:
    print("\n" + "=" * 68)
    print(f"{n}) {title}")
    print("=" * 68)


def scenario_allowlist(w) -> None:
    from agentpay import Session, PolicyRejected
    _banner(1, "RECIPIENT ALLOWLIST  ->  PAYEE NOT LISTED, NOTHING SIGNED")
    s = Session(wallet=w, gateway_url=GATEWAY, max_spend="0.05", prefer_chain="stacks",
                allowed_recipients=[NOT_THE_PAYEE])
    print(f"allowed_recipients=[{NOT_THE_PAYEE[:12]}…]   calling {TOOL} ...")
    try:
        s.call(TOOL, PARAMS)
        print("  x  UNEXPECTED: the call was not refused"); sys.exit(1)
    except PolicyRejected as e:
        print(f"  ✓ refused before signing — rule={e.rule} chain={e.chain} payee={e.pay_to[:12]}…")
    print("  spent:", s.spent(), "| anomalies:", s.anomalies())


def scenario_per_call(w) -> None:
    from agentpay import Session, PolicyRejected
    _banner(2, "PER-CALL MAXIMUM  ->  OVER THE RULE, REFUSED ON THE QUOTE")
    s = Session(wallet=w, gateway_url=GATEWAY, max_spend="0.05", prefer_chain="stacks",
                max_per_call="0.005")
    print(f"max_per_call=$0.005 (tool is $0.01)   calling {TOOL} ...")
    try:
        s.call(TOOL, PARAMS)
        print("  x  UNEXPECTED: the call was not refused"); sys.exit(1)
    except PolicyRejected as e:
        print(f"  ✓ refused — rule={e.rule} amount=${e.amount}")
    print("  spent:", s.spent(), "| anomalies:", s.anomalies())


def scenario_approval(w) -> None:
    from agentpay import Session, ApprovalRequired, SettlementUncertain, PaymentFailed
    _banner(3, "APPROVAL GATE  ->  DENIED, THEN APPROVED AND SETTLED IN sBTC")

    def deny(req):
        print(f"  approver asked: {req}  -> no")
        return False

    s = Session(wallet=w, gateway_url=GATEWAY, max_spend="0.05", prefer_chain="stacks",
                approve_above="0.005", approver=deny)
    print(f"approve_above=$0.005 (tool is $0.01)   calling {TOOL} ...")
    try:
        s.call(TOOL, PARAMS)
        print("  x  UNEXPECTED: the call was not held for approval"); sys.exit(1)
    except ApprovalRequired as e:
        print(f"  ✓ held — {str(e)[:120]}")
    print("  spent:", s.spent(), "| anomalies:", s.anomalies())

    def allow(req):
        print(f"  approver asked: {req}  -> yes")
        return True

    s2 = Session(wallet=w, gateway_url=GATEWAY, max_spend="0.05", prefer_chain="stacks",
                 approve_above="0.005", approver=allow, allowed_recipients=None)
    print(f"\nsame rule, approver says yes   calling {TOOL} ...")
    try:
        r = s2.call(TOOL, PARAMS)
        tx = getattr(r, "tx", None)
        print("  ✓ SETTLED — tx", tx)
        if tx:
            print(f"  verify : {EXPLORER}/txid/0x{str(tx).removeprefix('0x')}?chain={NETWORK}")
    except SettlementUncertain as e:
        print("  ✓ BROADCAST, confirming — tx", e.tx_hash, "(redeemable: s.redeem(e))")
    except PaymentFailed as e:
        print("  x payment failed:", str(e)[:200]); sys.exit(1)
    _banner(4, "THE RECEIPT")
    print(s2.spending_summary())


if __name__ == "__main__":
    w = _wallet()
    print(f"payer (Stacks {NETWORK}): {w.stacks_address}   gateway: {GATEWAY}")
    scenario_allowlist(w)
    scenario_per_call(w)
    scenario_approval(w)
    print("\nDone. Three refusals with nothing signed; one approved sBTC settlement.")
