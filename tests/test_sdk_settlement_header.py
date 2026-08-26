"""
AGE-142 — the SDK reads the x402 settlement header on external paid calls.

Third-party sellers return the settlement as PAYMENT-RESPONSE (v2) /
X-PAYMENT-RESPONSE (v1): base64 JSON {success, transaction, network, payer}.
Before this the SDK only read our own JSON envelope, so every off-gateway leg
carried tx_hash="" (0 of 106 settled prober legs had one) and the ledger could
never verify them.
"""
from __future__ import annotations

import base64
import json
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import respx

from agentpay._wallet import Session, _settlement_from_headers

GATEWAY = "https://gw.example"
URL = "https://ext.example/tool"


def _b64(obj: dict, urlsafe: bool = False, strip_pad: bool = False) -> str:
    raw = json.dumps(obj).encode()
    s = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(raw).decode()
    return s.rstrip("=") if strip_pad else s


def test_parse_v2_header_standard_b64():
    h = {"PAYMENT-RESPONSE": _b64({"success": True, "transaction": "0xABC",
                                   "network": "eip155:8453", "payer": "0xp"})}
    out = _settlement_from_headers(h)
    assert out == {"tx_hash": "0xABC", "network": "eip155:8453", "payer": "0xp", "success": True}


def test_parse_v1_header_urlsafe_no_padding_and_alt_keys():
    h = {"X-PAYMENT-RESPONSE": _b64({"success": True, "txHash": "0xdef", "network": "base"},
                                    urlsafe=True, strip_pad=True)}
    assert _settlement_from_headers(h)["tx_hash"] == "0xdef"


def test_parse_plain_json_and_garbage():
    assert _settlement_from_headers({"PAYMENT-RESPONSE": '{"transaction":"0x1"}'})["tx_hash"] == "0x1"
    assert _settlement_from_headers({"PAYMENT-RESPONSE": "!!not-b64!!"}) is None
    assert _settlement_from_headers({}) is None
    assert _settlement_from_headers(None) is None


def _session_wallet():
    w = MagicMock()
    w.network = "mainnet"
    w.base_address = "0x" + "b" * 40
    w.build_base_payment_signature.return_value = "sig-b64"
    return w


def test_external_base_call_records_tx_hash_from_header():
    w = _session_wallet()
    s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
    accepts = {"accepts": [{"network": "eip155:8453", "amount": "1000",
                            "payTo": "0x" + "c" * 40, "scheme": "exact"}]}
    settle = _b64({"success": True, "transaction": "0x" + "e" * 64,
                   "network": "eip155:8453", "payer": w.base_address})
    with respx.mock:
        respx.post(URL).mock(side_effect=[
            httpx.Response(402, json=accepts),
            httpx.Response(200, json={"data": {"ok": 1}},
                           headers={"PAYMENT-RESPONSE": settle}),
        ])
        r = s.call(URL, {"q": "x"})
    entry = s.summary()["breakdown"][0]
    assert entry["tx_hash"] == "0x" + "e" * 64
    assert entry["state"] == "settled"
    assert entry.get("settlement_source") == "header"
    assert s.spent_usd() == Decimal("0.001000")
    # The result envelope carries it too (what the prober reads as r.tx).
    assert (r.get("payment") or {}).get("tx_hash") == "0x" + "e" * 64


def test_external_base_call_body_envelope_still_wins():
    w = _session_wallet()
    s = Session(w, gateway_url=GATEWAY, max_spend="1.00")
    accepts = {"accepts": [{"network": "eip155:8453", "amount": "1000",
                            "payTo": "0x" + "c" * 40, "scheme": "exact"}]}
    with respx.mock:
        respx.post(URL).mock(side_effect=[
            httpx.Response(402, json=accepts),
            httpx.Response(200, json={"payment": {"tx_hash": "0xbody"}},
                           headers={"PAYMENT-RESPONSE": _b64({"transaction": "0xhdr"})}),
        ])
        s.call(URL, {"q": "x"})
    assert s.summary()["breakdown"][0]["tx_hash"] == "0xbody"
