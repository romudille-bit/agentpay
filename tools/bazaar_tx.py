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
AMOUNT_ATOMIC   = 10000     # $0.01 USDC (6 decimals)


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
    # ResourceInfo — required for Bazaar indexing.
    # service_name ≤ 32 chars; tags ≤ 5 entries, each ≤ 32 chars.
    "resource": {
        "url":         SESSION_URL,
        "description": "Open a budget-capped agent session. Pay $0.01 USDC once — get a session_id and access to 17 free crypto data tools.",
        "mimeType":    "application/json",
        "serviceName": "AgentPay",
        "tags":        ["ai-agents", "crypto", "defi", "session", "budget"],
    },
    "accepted": {
        "scheme":            "exact",
        "network":           CAIP2_NETWORK,
        "amount":            str(AMOUNT_ATOMIC),
        "asset":             USDC_BASE,
        "payTo":             GATEWAY_ADDRESS,
        "maxTimeoutSeconds": 300,
        "resource":          SESSION_URL,
        "description":       "Open a budget-capped agent session on AgentPay. Pay $0.01 USDC once — get a session_id, budget config, and access to 17 free crypto data tools.",
        "mimeType":          "application/json",
        "extra": {
            "name":                "USD Coin",
            "version":             "2",
            "assetTransferMethod": "eip3009",
        },
    },
    # Bazaar extension — required for CDP to index this resource in discovery.
    # info.input must be the full HTTP input object (type + method + body).
    # schema.properties.input must validate info.input exactly.
    "extensions": {
        "bazaar": {
            "info": {
                "input": {
                    "type":     "http",
                    "method":   "POST",
                    "bodyType": "json",
                    "body": {
                        "agent_address": agent_address,
                        "max_spend":     "0.10",
                        "label":         "bazaar-indexing",
                    },
                },
                "output": {
                    "type": "json",
                    "example": {
                        "session_id":     "f47ac10b-58cc-4372-a567-0e02b2c3d479",
                        "max_spend":      "0.10",
                        "gateway_url":    GATEWAY_URL,
                        "tools_endpoint": f"{GATEWAY_URL}/tools",
                        "created_at":     "2026-05-29T00:00:00Z",
                        "receipt": {
                            "tx_hash":     "0xee85d8dd374b5d1cb40bfa441086af557d356acc2bb4d5819f56331fce42adee",
                            "network":     "eip155:8453",
                            "amount_usdc": "0.01",
                        },
                    },
                },
            },
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "input": {
                        "type": "object",
                        "required": ["type", "method", "body"],
                        "properties": {
                            "type":     {"type": "string"},
                            "method":   {"type": "string"},
                            "bodyType": {"type": "string"},
                            "body": {
                                "type": "object",
                                "required": ["agent_address"],
                                "properties": {
                                    "agent_address": {"type": "string", "description": "Paying agent's EVM wallet address"},
                                    "max_spend":     {"type": "string", "description": "Hard budget cap in USDC, e.g. '0.10'"},
                                    "label":         {"type": "string", "description": "Optional human-readable session label"},
                                },
                            },
                        },
                    },
                },
            },
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
