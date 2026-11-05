"""PAYMENT-RESPONSE: the x402 settlement receipt header on a paid 200.

Stock x402 clients and third-party verifiers read the receipt from this
header, not from our JSON body: base64(JSON) of
{"success", "transaction", "network" (CAIP-2), "payer"}. `X-PAYMENT-RESPONSE`
is the v1 spelling; both are sent. The settle paths return internal network
labels (`stellar-mainnet`, `base`, ...), mapped to CAIP-2 here; unknown
labels pass through, since an odd network string beats no receipt.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Mapping, Optional

from agentpay._stacks_tx import STACKS_MAINNET_CAIP2, STACKS_TESTNET_CAIP2
from gateway.base import CAIP2_BASE_MAINNET, CAIP2_BASE_SEPOLIA
from gateway.config import settings, stellar_caip2

HEADER_V2 = "PAYMENT-RESPONSE"
HEADER_V1 = "X-PAYMENT-RESPONSE"

_LABEL_TO_CAIP2 = {
    "base":           CAIP2_BASE_MAINNET,
    "base-mainnet":   CAIP2_BASE_MAINNET,
    "base-sepolia":   CAIP2_BASE_SEPOLIA,
    "stacks-mainnet": STACKS_MAINNET_CAIP2,
    "stacks-testnet": STACKS_TESTNET_CAIP2,
}


def caip2_of(network_label: Optional[str]) -> str:
    """Internal network label → CAIP-2 id. Already-CAIP-2 strings pass through."""
    label = (network_label or "").strip()
    if not label:
        return stellar_caip2()
    if ":" in label:                      # eip155:8453, stellar:pubnet, stacks:1
        return label
    if label.startswith("stellar"):
        return stellar_caip2()
    if label.startswith("stacks") and label not in _LABEL_TO_CAIP2:
        return (STACKS_MAINNET_CAIP2 if settings.STACKS_NETWORK == "mainnet"
                else STACKS_TESTNET_CAIP2)
    return _LABEL_TO_CAIP2.get(label, label)


def encode(tx_hash: str, network_label: Optional[str], payer: Optional[str]) -> str:
    body = {
        "success":     True,
        "transaction": tx_hash or "",
        "network":     caip2_of(network_label),
        "payer":       payer or "",
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()


def headers_for(auth: Mapping[str, Any]) -> dict[str, str]:
    """Headers for a paid 200, from a settle-path `auth` dict
    ({authorized, tx_hash, payer, network, ...}). Empty for an unpaid call:
    a $0 settle has no transaction, and a receipt that names none would be
    read as a failed settlement by a strict client."""
    tx = str(auth.get("tx_hash") or "")
    if not auth.get("authorized") or not tx:
        return {}
    value = encode(tx, auth.get("network"), auth.get("payer"))
    return {HEADER_V2: value, HEADER_V1: value}
