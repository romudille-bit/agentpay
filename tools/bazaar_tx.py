#!/usr/bin/env python3
"""
tools/bazaar_tx.py — Trigger Bazaar auto-indexing via CDP Facilitator (Mode A).

What this does:
  1. Hits POST /v1/session/create → gets 402 + Base payment requirements
  2. Signs an EIP-3009 transferWithAuthorization off-chain (no gas needed)
  3. Sends the signed payload to the gateway as PAYMENT-SIGNATURE
  4. Gateway routes it through the CDP Facilitator at api.cdp.coinbase.com
  5. CDP submits the on-chain tx, reads resource_url → Bazaar indexes AgentPay
  6. Prints session_id + tx receipt

After this runs once, agentpay.tools/v1/session/create appears on Base Bazaar.

Requirements:
  pip install requests eth-account --break-system-packages

Usage:
  python3 tools/bazaar_tx.py
  (prompts for private key — never stored, never echoed)
"""

import base64
import getpass
import json
import secrets
import sys
import time

import requests
from eth_account import Account

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_URL     = "https://agentpay.tools"
SESSION_URL     = f"{GATEWAY_URL}/v1/session/create"
USDC_BASE       = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GATEWAY_ADDRESS = "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7"
CHAIN_ID        = 8453
AMOUNT_ATOMIC   = 1000     # $0.001 USDC (6 decimals)
CAIP2_NETWORK   = "eip155:8453"

# ── Load private key ──────────────────────────────────────────────────────────

private_key   = getpass.getpass("Base private key (0x...): ").strip()
if not private_key:
    print("Error: no key provided.")
    sys.exit(1)

account       = Account.from_key(private_key)
agent_address = account.address

print(f"\nAgent address : {agent_address}")
print(f"Gateway       : {GATEWAY_URL}\n")


# ── Step 1: Get 402 challenge ─────────────────────────────────────────────────

print("Step 1 — Fetching 402 challenge...")
resp = requests.post(
    SESSION_URL,
    json={"max_spend": "0.10", "agent_address": agent_address, "label": "bazaar-indexing"},
    timeout=15,
)

if resp.status_code != 402:
    print(f"Expected 402, got {resp.status_code}: {resp.text}")
    sys.exit(1)

challenge   = resp.json()
base_option = challenge.get("payment_options", {}).get("base", {})

if not base_option:
    print("No Base payment option in 402 response.")
    print(json.dumps(challenge, indent=2))
    sys.exit(1)

print(f"  payment_id : {challenge.get('payment_id')}\n")


# ── Step 2: Sign EIP-3009 transferWithAuthorization ──────────────────────────

print("Step 2 — Signing EIP-3009 transferWithAuthorization (off-chain, no gas)...")

nonce_bytes  = secrets.token_bytes(32)
nonce_hex    = "0x" + nonce_bytes.hex()
valid_after  = 0
valid_before = int(time.time()) + 300

domain = {
    "name":              "USD Coin",
    "version":           "2",
    "chainId":           CHAIN_ID,
    "verifyingContract": USDC_BASE,
}

types = {
    "TransferWithAuthorization": [
        {"name": "from",         "type": "address"},
        {"name": "to",           "type": "address"},
        {"name": "value",        "type": "uint256"},
        {"name": "validAfter",   "type": "uint256"},
        {"name": "validBefore",  "type": "uint256"},
        {"name": "nonce",        "type": "bytes32"},
    ],
}

message = {
    "from":        agent_address,
    "to":          GATEWAY_ADDRESS,
    "value":       AMOUNT_ATOMIC,
    "validAfter":  valid_after,
    "validBefore": valid_before,
    "nonce":       nonce_bytes,
}

signed  = Account.sign_typed_data(account.key, domain, types, message)
sig_hex = signed.signature.hex()
if not sig_hex.startswith("0x"):
    sig_hex = "0x" + sig_hex

print(f"  nonce      : {nonce_hex[:18]}...")
print(f"  validBefore: {valid_before}")
print(f"  signature  : {sig_hex[:20]}...\n")


# ── Step 3: Build PAYMENT-SIGNATURE (Mode A / CDP format) ────────────────────

print("Step 3 — Building CDP payload...")

payment_payload = {
    "x402Version": 2,
    # Inner EIP-3009 data — matches ExactEIP3009Payload.to_dict()
    "payload": {
        "authorization": {
            "from":        agent_address,
            "to":          GATEWAY_ADDRESS,
            "value":       str(AMOUNT_ATOMIC),
            "validAfter":  str(valid_after),
            "validBefore": str(valid_before),
            "nonce":       nonce_hex,
        },
        "signature": sig_hex,
    },
    # Payment requirements — camelCase, matches PaymentRequirements schema
    "accepted": {
        "scheme":             "exact",
        "network":            CAIP2_NETWORK,
        "amount":             str(AMOUNT_ATOMIC),
        "asset":              USDC_BASE,
        "payTo":              GATEWAY_ADDRESS,
        "maxTimeoutSeconds":  300,
        "resource":           SESSION_URL,
        "description":        "AgentPay session_create",
        "mimeType":           "application/json",
        "extra": {
            "name":                "USDC",
            "version":             "2",
            "assetTransferMethod": "eip3009",
        },
    },
}

payment_sig = base64.b64encode(json.dumps(payment_payload).encode()).decode()
print(f"  payload    : {len(payment_sig)} bytes\n")


# ── Step 4: Submit → gateway → CDP Facilitator → Bazaar ──────────────────────

print("Step 4 — Submitting to gateway (CDP Facilitator path)...")
print("  CDP will submit the tx on-chain. Allow 10–20s...\n")

retry = requests.post(
    SESSION_URL,
    json={"max_spend": "0.10", "agent_address": agent_address, "label": "bazaar-indexing"},
    headers={
        "PAYMENT-SIGNATURE": payment_sig,
        "X-Agent-Address":   agent_address,
        "Content-Type":      "application/json",
    },
    timeout=40,
)

print(f"Status: {retry.status_code}\n")

if retry.status_code == 200:
    result  = retry.json()
    receipt = result.get("receipt", {})
    print("✅ Success — CDP Facilitator settled, Bazaar indexing triggered\n")
    print(f"  session_id  : {result.get('session_id')}")
    print(f"  tx_hash     : {receipt.get('tx_hash')}")
    print(f"  network     : {receipt.get('network')}")
    print(f"  amount_usdc : ${receipt.get('amount_usdc')}")
    print(f"\nVerify on Basescan:")
    print(f"  https://basescan.org/tx/{receipt.get('tx_hash')}")
    print(f"\nBazaar (allow 5–10 min to index):")
    print(f"  https://app.base.org/bazaar — search 'AgentPay'")
else:
    print("❌ Failed:")
    try:
        print(json.dumps(retry.json(), indent=2))
    except Exception:
        print(retry.text)
