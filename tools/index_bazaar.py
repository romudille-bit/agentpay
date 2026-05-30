#!/usr/bin/env python3
"""
tools/index_bazaar.py — trigger Bazaar auto-indexing with a real session_create
payment on Base mainnet (gasless EIP-3009 via the CDP Facilitator).

Deliberately sends NO Bazaar `resource`/`extensions` in the client payload — the
gateway now injects its canonical indexing metadata server-side, so a plain
payment proves that fix works end-to-end.

Setup:
  export AGENT_BASE_KEY_TEST=0x...        # funded Base wallet (needs ~$0.01 USDC)
  pip install requests eth-account "x402[evm]"

Run:
  python3 tools/index_bazaar.py
"""

import base64
import json
import os
import sys
import time

import requests
from eth_account import Account


# ── Load .env (no external dep) ────────────────────────────────────────────────
def _load_dotenv():
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    try:
        with open(env_path) as fh:
            for raw in fh:
                s = raw.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

_load_dotenv()

# Gateway: DEMO_GATEWAY override, else custom domain, else Railway fallback.
def _pick_gateway():
    for g in [os.environ.get("DEMO_GATEWAY"),
              "https://agentpay.tools",
              "https://gateway-production-2cc2.up.railway.app"]:
        if not g:
            continue
        g = g.rstrip("/")
        try:
            requests.get(f"{g}/health", timeout=8)
            return g
        except Exception:
            continue
    return "https://agentpay.tools"

GATEWAY      = _pick_gateway()
SESSION_URL  = f"{GATEWAY}/v1/session/create"
USDC_BASE    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GATEWAY_ADDR = "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7"
CAIP2        = "eip155:8453"
AMOUNT       = 1000   # $0.001 USDC (6 decimals)

# ── Wallet ─────────────────────────────────────────────────────────────────────
hex_key = os.environ.get("AGENT_BASE_KEY_TEST", "").strip().lower().removeprefix("0x")
if len(hex_key) != 64:
    print("✗ AGENT_BASE_KEY_TEST not set (or not a 64-char hex key). Set it and retry.")
    sys.exit(1)
acct = Account.from_key("0x" + hex_key)
print(f"\nGateway : {GATEWAY}")
print(f"Agent   : {acct.address}\n")

try:
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.mechanisms.evm.exact.client import ExactEvmScheme
    from x402.schemas import PaymentRequirements
except ImportError:
    print('✗ Missing dep — run  pip install "x402[evm]"')
    sys.exit(1)

# ── Step 1: 402 challenge ──────────────────────────────────────────────────────
print("1/3 — requesting session_create (expect 402)...")
r = requests.post(SESSION_URL,
                  json={"max_spend": "0.10", "agent_address": acct.address, "label": "bazaar-index"},
                  timeout=15)
if r.status_code != 402:
    print(f"✗ expected 402, got {r.status_code}: {r.text[:200]}")
    sys.exit(1)
print(f"    payment_id: {r.json().get('payment_id')}")

# ── Step 2: sign EIP-3009 (off-chain, gasless) ─────────────────────────────────
print("2/3 — signing EIP-3009 (off-chain, no gas)...")
scheme = ExactEvmScheme(EthAccountSigner(acct))
requirements = PaymentRequirements(
    scheme="exact", network=CAIP2, asset=USDC_BASE, amount=str(AMOUNT),
    pay_to=GATEWAY_ADDR, max_timeout_seconds=300,
    extra={"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"},
)
# NOTE: no `resource`, no `extensions` here on purpose — the gateway injects them.
payment_payload = {
    "x402Version": 2,
    "payload": scheme.create_payment_payload(requirements),
    "accepted": {
        "scheme": "exact", "network": CAIP2, "amount": str(AMOUNT), "asset": USDC_BASE,
        "payTo": GATEWAY_ADDR, "maxTimeoutSeconds": 300, "resource": SESSION_URL,
        "mimeType": "application/json",
        "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"},
    },
}
payment_sig = base64.b64encode(json.dumps(payment_payload).encode()).decode()

# ── Step 3: submit → CDP Facilitator settles → Bazaar indexes ──────────────────
print("3/3 — submitting (CDP settles on-chain, ~10-20s)...\n")
paid = requests.post(SESSION_URL,
                     json={"max_spend": "0.10", "agent_address": acct.address, "label": "bazaar-index"},
                     headers={"PAYMENT-SIGNATURE": payment_sig, "X-Agent-Address": acct.address},
                     timeout=60)

if paid.status_code != 200:
    print(f"✗ payment failed ({paid.status_code}):")
    try:
        print(json.dumps(paid.json(), indent=2))
    except Exception:
        print(paid.text[:400])
    sys.exit(1)

receipt = paid.json().get("receipt", {})
tx = receipt.get("tx_hash", "")
print("✓ Settled on Base via CDP Facilitator — Bazaar indexing should now fire.\n")
print(f"  tx_hash : {tx}")
print(f"  network : {receipt.get('network')}")
print(f"  amount  : ${receipt.get('amount_usdc')} USDC")
print(f"  verify  : https://basescan.org/tx/{tx}\n")
print("Next:")
print("  1) Check Railway logs for:  [BASE] Bazaar extension response: {...}")
print("     (that's Coinbase's accept/reject verdict on the indexing extension)")
print("  2) Wait ~5-10 min, then:")
print('     curl "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=agentpay"')
