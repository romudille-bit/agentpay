#!/usr/bin/env python3
"""
tools/index_bazaar_tool.py — trigger Bazaar auto-indexing for a PAID TOOL's
/call endpoint (default: verified_route) with a real Mode-A settle on Base
mainnet (gasless EIP-3009 via the CDP Facilitator).

Sibling of tools/index_bazaar.py (which settles /v1/session/create). Same flow,
generalized to any paid tool's POST /tools/<name>/call. Sends NO Bazaar
resource/extensions in the client payload — the gateway injects its canonical
indexing metadata server-side (from _TOOL_BAZAAR), so a plain settle proves the
live-402 + settle extension wiring end-to-end.

Indexing only fires AFTER the gateway is deployed with the tool's _TOOL_BAZAAR
entry (so its live 402 carries extensions.bazaar) and the settle is Mode A (CDP).

Setup:
  export AGENT_BASE_KEY_TEST=0x...        # funded Base wallet (needs ~$0.01 USDC)
  pip install requests eth-account "x402[evm]"

Run:
  python3 tools/index_bazaar_tool.py                       # verified_route
  TARGET_TOOL=verified_route python3 tools/index_bazaar_tool.py
  TARGET_TOOL=pre_trade_check TOOL_PARAMS='{"symbol":"ETH"}' python3 tools/index_bazaar_tool.py
"""

import base64
import json
import os
import sys

import requests
from eth_account import Account


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

GATEWAY      = os.environ.get("AGENTPAY_GATEWAY_URL", "https://agentpay.tools").rstrip("/")
TARGET_TOOL  = os.environ.get("TARGET_TOOL", "verified_route").strip()
TOOL_PARAMS  = json.loads(os.environ.get("TOOL_PARAMS", '{"need": "dex pair liquidity"}'))
CALL_URL     = f"{GATEWAY}/tools/{TARGET_TOOL}/call"
USDC_BASE    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GATEWAY_ADDR = "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7"
CAIP2        = "eip155:8453"
AMOUNT       = 10000   # $0.01 USDC (6 decimals) — paid tools are $0.01

hex_key = os.environ.get("AGENT_BASE_KEY_TEST", "").strip().lower().removeprefix("0x")
if len(hex_key) != 64:
    print("✗ AGENT_BASE_KEY_TEST not set (or not a 64-char hex key). Set it and retry.")
    sys.exit(1)
acct = Account.from_key("0x" + hex_key)
print(f"\nGateway : {GATEWAY}")
print(f"Tool    : {TARGET_TOOL}  ({CALL_URL})")
print(f"Agent   : {acct.address}\n")

try:
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.mechanisms.evm.exact.client import ExactEvmScheme
    from x402.schemas import PaymentRequirements
except ImportError:
    print('✗ Missing dep — run  pip install "x402[evm]"')
    sys.exit(1)

body = {"parameters": TOOL_PARAMS, "agent_address": acct.address}

# ── Step 1: 402 challenge ──────────────────────────────────────────────────────
print(f"1/3 — requesting {TARGET_TOOL} (expect 402)...")
r = requests.post(CALL_URL, json=body, timeout=20)
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
# No `resource`/`extensions` here on purpose — the gateway injects them server-side.
payment_payload = {
    "x402Version": 2,
    "payload": scheme.create_payment_payload(requirements),
    "accepted": {
        "scheme": "exact", "network": CAIP2, "amount": str(AMOUNT), "asset": USDC_BASE,
        "payTo": GATEWAY_ADDR, "maxTimeoutSeconds": 300, "resource": CALL_URL,
        "mimeType": "application/json",
        "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"},
    },
}
payment_sig = base64.b64encode(json.dumps(payment_payload).encode()).decode()

# ── Step 3: submit → CDP Facilitator settles → Bazaar indexes ──────────────────
print("3/3 — submitting (CDP settles on-chain, ~10-20s)...\n")
paid = requests.post(CALL_URL, json=body,
                     headers={"PAYMENT-SIGNATURE": payment_sig, "X-Agent-Address": acct.address},
                     timeout=90)

if paid.status_code != 200:
    print(f"✗ payment failed ({paid.status_code}):")
    try:
        print(json.dumps(paid.json(), indent=2))
    except Exception:
        print(paid.text[:400])
    sys.exit(1)

receipt = paid.json().get("receipt", {})
tx = receipt.get("tx_hash", "")
print(f"✓ Settled {TARGET_TOOL} on Base via CDP — Bazaar indexing should now fire.\n")
print(f"  tx_hash : {tx}")
print(f"  network : {receipt.get('network')}")
print(f"  amount  : ${receipt.get('amount_usdc')} USDC")
print(f"  verify  : https://basescan.org/tx/{tx}\n")
print("Next:")
print("  1) Railway logs for:  [BASE] Bazaar extension response: {...}")
print("  2) Wait ~5-10 min, then:")
print('     curl "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=verified%20route"')
print('     curl "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=agentpay"')
