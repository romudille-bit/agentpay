#!/usr/bin/env python3
"""
tools/bazaar_tx.py — Trigger the first AgentPay session_create via Base/CDP Facilitator.

What this does:
  1. Hits POST /v1/session/create → gets 402 + payment requirements
  2. Signs an EIP-3009 transferWithAuthorization off-chain (no gas yet)
  3. Sends the signed payload to the gateway as PAYMENT-SIGNATURE header
  4. Gateway routes it through the CDP Facilitator at api.cdp.coinbase.com
  5. CDP submits the on-chain tx, reads resource_url → Bazaar indexes AgentPay
  6. Prints session_id + tx receipt

After this runs once, agentpay.tools/v1/session/create appears on Base Bazaar
and agents can discover it autonomously.

Requirements:
  pip install requests eth-account --break-system-packages

Usage:
  python3 tools/bazaar_tx.py
  (prompts for private key — never stored, never echoed)
"""

import base64
import getpass
import json
import os
import secrets
import sys
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_URL     = "https://agentpay.tools"
SESSION_URL     = f"{GATEWAY_URL}/v1/session/create"
USDC_BASE       = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # USDC on Base mainnet
GATEWAY_ADDRESS = "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7"   # AgentPay gateway
CHAIN_ID        = 8453     # Base mainnet
AMOUNT_ATOMIC   = 1000     # $0.001 USDC — 6 decimal places (1000 = 0.001 * 10^6)
CAIP2_NETWORK   = "eip155:8453"

# ── Load private key (interactive, never echoed) ──────────────────────────────

private_key = getpass.getpass("Base private key (0x...): ").strip()
if not private_key:
    print("Error: no key provided.")
    sys.exit(1)

account = Account.from_key(private_key)
agent_address = account.address
session_label = "bazaar-bootstrap"

print(f"Agent address : {agent_address}")
print(f"Session label : {session_label}")
print(f"Gateway       : {GATEWAY_URL}")
print(f"Amount        : $0.001 USDC on Base mainnet\n")


# ── Step 1: Get 402 challenge ─────────────────────────────────────────────────

print("Step 1 — Fetching 402 challenge...")
resp = requests.post(
    SESSION_URL,
    json={"max_spend": "0.10", "agent_address": agent_address, "label": session_label},
    timeout=15,
)

if resp.status_code != 402:
    print(f"Expected 402, got {resp.status_code}:")
    print(resp.text)
    sys.exit(1)

challenge = resp.json()
payment_id = challenge.get("payment_id", "")
base_option = challenge.get("payment_options", {}).get("base", {})

if not base_option:
    print("No Base payment option in 402 response. Is BASE_GATEWAY_ADDRESS set on the gateway?")
    print(json.dumps(challenge, indent=2))
    sys.exit(1)

print(f"  payment_id : {payment_id}")
print(f"  pay_to     : {base_option.get('pay_to', GATEWAY_ADDRESS)}")
print(f"  amount     : {base_option.get('amount_usdc')} USDC\n")


# ── Step 2: Sign EIP-3009 transferWithAuthorization ──────────────────────────

print("Step 2 — Signing EIP-3009 transferWithAuthorization (off-chain, no gas)...")

# Random 32-byte nonce — prevents replay of this exact authorization
nonce_bytes = secrets.token_bytes(32)
nonce_hex   = "0x" + nonce_bytes.hex()

valid_after  = 0                        # valid from the epoch
valid_before = int(time.time()) + 300   # 5 minute window

# EIP-712 typed data for USDC transferWithAuthorization on Base mainnet
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
    "nonce":       nonce_bytes,      # eth_account accepts raw bytes for bytes32
}

signed = Account.sign_typed_data(
    private_key=account.key,
    domain_data=domain,
    message_types=types,
    message_data=message,
)

# Compact 65-byte hex signature (v, r, s)
sig_hex = signed.signature.hex()
if not sig_hex.startswith("0x"):
    sig_hex = "0x" + sig_hex

print(f"  nonce      : {nonce_hex[:18]}...")
print(f"  validBefore: {valid_before} ({time.strftime('%H:%M:%S', time.gmtime(valid_before))} UTC)")
print(f"  signature  : {sig_hex[:20]}...\n")


# ── Step 3: Build PAYMENT-SIGNATURE payload (CDP Mode A format) ──────────────
#
# The gateway's settle_base_payment() detects "payload" key → Mode A → CDP.
# It forwards the whole dict as paymentPayload to:
#   POST https://api.cdp.coinbase.com/platform/v2/x402/settle
# CDP submits the tx on-chain, reads paymentRequirements.resource → Bazaar
# indexes that URL automatically.

print("Step 3 — Building PAYMENT-SIGNATURE payload...")

payment_payload = {
    "x402Version": 2,
    "scheme":      "exact",
    "network":     CAIP2_NETWORK,
    # "payload" key triggers Mode A detection in gateway/base.py
    "payload": {
        "signature": sig_hex,
        "authorization": {
            "from":        agent_address,
            "to":          GATEWAY_ADDRESS,
            "value":       str(AMOUNT_ATOMIC),
            "validAfter":  str(valid_after),
            "validBefore": str(valid_before),
            "nonce":       nonce_hex,
        },
    },
    # paymentRequirements are re-built server-side, but include here
    # so the gateway can cross-check before forwarding to CDP.
    "paymentRequirements": {
        "scheme":            "exact",
        "network":           CAIP2_NETWORK,
        "amount":            str(AMOUNT_ATOMIC),
        "asset":             USDC_BASE,
        "payTo":             GATEWAY_ADDRESS,
        "maxTimeoutSeconds": 300,
        "resource":          SESSION_URL,
        "description":       "AgentPay session_create",
        "mimeType":          "application/json",
        "extra": {
            "name":                "USDC",
            "version":             "2",
            "assetTransferMethod": "eip3009",
        },
    },
}

payment_sig_header = base64.b64encode(
    json.dumps(payment_payload).encode()
).decode()

print(f"  payload size: {len(payment_sig_header)} bytes (base64)\n")


# ── Step 4: Retry with PAYMENT-SIGNATURE → gateway → CDP → Bazaar ────────────

print("Step 4 — Submitting to gateway (CDP Facilitator path)...")
print("  This may take 5–15 seconds while CDP submits the on-chain tx...\n")

retry = requests.post(
    SESSION_URL,
    json={
        "max_spend":    "0.10",
        "agent_address": agent_address,
        "label":         session_label,
    },
    headers={
        "PAYMENT-SIGNATURE": payment_sig_header,
        "X-Agent-Address":   agent_address,
        "Content-Type":      "application/json",
    },
    timeout=30,
)

print(f"Status: {retry.status_code}\n")

if retry.status_code == 200:
    result = retry.json()
    print("✅ Success — session created, Bazaar indexing triggered\n")
    print(f"  session_id  : {result.get('session_id')}")
    print(f"  max_spend   : ${result.get('max_spend')}")
    print(f"  created_at  : {result.get('created_at')}")
    receipt = result.get("receipt", {})
    print(f"  tx_hash     : {receipt.get('tx_hash')}")
    print(f"  network     : {receipt.get('network')}")
    print(f"  amount_usdc : ${receipt.get('amount_usdc')}")
    print(f"\nVerify on Basescan:")
    print(f"  https://basescan.org/tx/{receipt.get('tx_hash')}")
    print(f"\nBazaar discovery (allow 5–10 min for indexing):")
    print(f"  https://app.base.org/bazaar")
    print(f"  Search: AgentPay  or  agentpay.tools")
else:
    print("❌ Payment failed:")
    try:
        print(json.dumps(retry.json(), indent=2))
    except Exception:
        print(retry.text)
    print("\nCommon causes:")
    print("  - Gateway BASE_GATEWAY_ADDRESS not set in Railway env")
    print("  - CDP Facilitator rejected the EIP-3009 signature")
    print("  - Insufficient USDC balance at", agent_address)
    print("  - USDC not approved (Base USDC uses EIP-3009, no approve() needed)")
