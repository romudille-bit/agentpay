#!/usr/bin/env python3
"""
tools/probe_cmc_settle.py — DEFINITIVE live test of the CMC x402 paid leg.

Settles ONE $0.01 `dex_search` against CMC via the agentpay-x402 SDK (Base,
EIP-3009, signed OFF-CHAIN — if CMC rejects the retry, NO USDC moves). This is
the only thing that confirms whether the SDK's POST-paid call returns data even
though CMC declares the endpoint as GET.

Reads the funded wallet from .env: FLAGSHIP_STELLAR_SECRET (identity) +
FLAGSHIP_BASE_KEY (or AGENT_BASE_KEY_TEST — the funded Base payer).

Run:
    ./venv/bin/python tools/probe_cmc_settle.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Load .env (no external dep) ────────────────────────────────────────────────
try:
    for _line in open(os.path.join(ROOT, ".env")):
        _s = _line.strip()
        if _s and not _s.startswith("#") and "=" in _s:
            _k, _, _v = _s.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
except FileNotFoundError:
    pass

sys.path.insert(0, ROOT)
from agentpay import AgentWallet, Session, PaymentFailed  # noqa: E402

base = (os.environ.get("FLAGSHIP_BASE_KEY")
        or os.environ.get("AGENT_BASE_KEY_TEST")
        or os.environ.get("BASE_AGENT_KEY") or "").strip()
if not base:
    print("✗ No funded Base key in .env (FLAGSHIP_BASE_KEY / AGENT_BASE_KEY_TEST / BASE_AGENT_KEY).")
    sys.exit(1)

# The settle is Base-only, so the Stellar key is just the wallet identity. Use any
# Stellar secret in .env; otherwise mint an ephemeral one (never used to pay).
stellar = (os.environ.get("FLAGSHIP_STELLAR_SECRET")
           or os.environ.get("AGENT_STELLAR_KEY_TEST") or "").strip()
if not stellar:
    from stellar_sdk import Keypair
    stellar = Keypair.random().secret
    print("(no Stellar secret in .env — using an ephemeral identity; payment settles on Base)")

wallet = AgentWallet(secret_key=stellar, network="mainnet", base_key=base)
print(f"Base payer : {wallet.base_address}")
s = Session(wallet=wallet, gateway_url="https://agentpay.tools", max_spend="0.05")

URL = "https://pro-api.coinmarketcap.com/x402/v1/dex/search?q=BNB"
print(f"Settling $0.01 dex_search via SDK (Base, off-chain EIP-3009)…\n  {URL}\n")

ok = False
try:
    r = s.call(URL)                       # default prefers Base; CMC offers Base USDC/eip3009
    data = r.data if hasattr(r, "data") else (r.get("result") if isinstance(r, dict) else r)
    print("✓ PAID POST RETURNED DATA — the CMC leg works over POST despite the GET declaration.")
    print("  tx:", getattr(r, "tx", None), "| network:", getattr(r, "network", None))
    print("  data (truncated):")
    print("   ", json.dumps(data, default=str)[:700])
    ok = True
except PaymentFailed as e:
    print(f"✗ PaymentFailed: {str(e)[:300]}")
except Exception as e:
    print(f"✗ call failed (no USDC moved if the Base retry was rejected off-chain):\n   {str(e)[:400]}")

print("\nreceipt:", s.spending_summary())
print("\nVERDICT:", "CMC paid leg CONFIRMED ✓ — strategy_spec can settle CMC over POST."
      if ok else "CMC rejected the POST — the SDK needs a GET path for external x402 URLs "
                 "(detect method from the 402's bazaar extension). Tell me and I'll add it.")
