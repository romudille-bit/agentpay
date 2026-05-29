#!/usr/bin/env python3
"""
tools/bazaar_tx.py — Trigger Bazaar auto-indexing via CDP Facilitator (Mode A).

Uses the official x402 Python SDK for signing — guarantees the exact
EIP-712 / EIP-3009 format the CDP Facilitator accepts.

Requirements:
  pip install requests eth-account "x402[evm]" --break-system-packages

Usage:
  python3 tools/bazaar_tx.py
  (prompts for private key — never stored, never echoed)
"""

import base64
import getpass
import json
import sys

import requests
from eth_account import Account

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_URL     = "https://agentpay.tools"
SESSION_URL     = f"{GATEWAY_URL}/v1/session/create"
USDC_BASE       = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GATEWAY_ADDRESS = "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7"
CAIP2_NETWORK   = "eip155:8453"
AMOUNT_ATOMIC   = 1000     # $0.001 USDC (6 decimals)


# ── Load private key ──────────────────────────────────────────────────────────

private_key = getpass.getpass("Base private key (0x...): ").strip()
if not private_key:
    print("Error: no key provided.")
    sys.exit(1)

hex_key = private_key.lower().removeprefix("0x")
if len(hex_key) != 64:
    print(f"Error: expected 64-char hex key (32 bytes), got {len(hex_key)} chars.")
    sys.exit(1)

eth_account = Account.from_key("0x" + hex_key)
agent_address = eth_account.address

print(f"\nAgent address : {agent_address}")
print(f"Gateway       : {GATEWAY_URL}\n")


# ── Import x402 SDK signing utilities ────────────────────────────────────────

try:
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.mechanisms.evm.exact.client import ExactEvmScheme
    from x402.schemas import PaymentRequirements
except ImportError:
    print('Error: x402[evm] not installed. Run: pip install "x402[evm]"')
    sys.exit(1)


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
print(f"  payment_id : {challenge.get('payment_id')}\n")


# ── Step 2: Sign using official x402 SDK ─────────────────────────────────────

print("Step 2 — Signing with x402 SDK (EIP-3009 / EIP-712)...")

signer = EthAccountSigner(eth_account)
scheme = ExactEvmScheme(signer)

requirements = PaymentRequirements(
    scheme="exact",
    network=CAIP2_NETWORK,
    asset=USDC_BASE,
    amount=str(AMOUNT_ATOMIC),
    pay_to=GATEWAY_ADDRESS,
    max_timeout_seconds=300,
    extra={
        "name":                "USD Coin",
        "version":             "2",
        "assetTransferMethod": "eip3009",
    },
)

payload_dict = scheme.create_payment_payload(requirements)
print(f"  signed authorization ready\n")


# ── Step 3: Build PAYMENT-SIGNATURE ──────────────────────────────────────────

print("Step 3 — Building PAYMENT-SIGNATURE...")

payment_payload = {
    "x402Version": 2,
    "payload":     payload_dict,
    "accepted": {
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
            "name":                "USD Coin",
            "version":             "2",
            "assetTransferMethod": "eip3009",
        },
    },
}

payment_sig = base64.b64encode(json.dumps(payment_payload).encode()).decode()
print(f"  payload    : {len(payment_sig)} bytes\n")


# ── Step 4: Submit → gateway → CDP Facilitator → Bazaar ──────────────────────

print("Step 4 — Submitting to gateway (CDP Facilitator path)...")
print("  Allow 10–20s for CDP to submit on-chain...\n")

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
    print(f"\nVerify: https://basescan.org/tx/{receipt.get('tx_hash')}")
    print(f"Bazaar (5–10 min): https://app.base.org/bazaar — search 'AgentPay'")
else:
    print("❌ Failed:")
    try:
        print(json.dumps(retry.json(), indent=2))
    except Exception:
        print(retry.text)
