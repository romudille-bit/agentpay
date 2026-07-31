"""
test_stacks_stale_nonce_requote.py

On a stale-nonce rejection the SDK must not re-sign with the same payment_id
(already consumed pre-broadcast → refused as a replay). It must re-request a
fresh 402 and re-sign against the new payment_id.
"""
import threading
from unittest.mock import MagicMock

import httpx
import respx

from agentpay._client import AgentPayClient

GATEWAY = "https://gateway-fake.example"
TOOL_URL = f"{GATEWAY}/tools/token_price/call"
PAY_TO = "ST1PAYTO000000000000000000000000000000000"


def _402(pid):
    return {
        "payment_id": pid,
        "amount_usdc": "0.01",
        "pay_to": PAY_TO,
        "payment_options": {
            "stacks": {
                "amount_sats": 9,
                "amount_usdc": "0.01",
                "pay_to": PAY_TO,
                "network": "stacks:2147483648",
                "btc_usd_rate": "118000",
            }
        },
    }


def _stacks_wallet():
    w = MagicMock()
    w.public_key = "GFAKEAGENT"
    w.network = "testnet"
    w.stacks_address = "ST1FAKEAGENT0000000000000000000000000000"
    w.stacks_disabled_reason = None
    w._stacks_lock = threading.Lock()
    seen = []

    def _build(stacks_opt, payment_id, url):
        seen.append(payment_id)
        return {
            "header": "hdr",
            "txid": f"tx-for-{payment_id}",
            "nonce": len(seen),
            "amount_sats": stacks_opt["amount_sats"],
            "amount_usd": stacks_opt.get("amount_usdc"),
        }

    w.build_stacks_payment.side_effect = _build
    w._seen = seen
    return w


def test_stale_nonce_requests_fresh_402_then_new_payment_id():
    wallet = _stacks_wallet()
    with respx.mock:
        respx.post(TOOL_URL).mock(side_effect=[
            httpx.Response(402, json=_402("pid-A")),                 # initial 402
            httpx.Response(409, json={"payment_status": "rejected",  # settle #1: stale nonce
                                      "error_reason": "broadcast rejected: bad nonce"}),
            httpx.Response(402, json=_402("pid-B")),                 # fresh 402 (re-request)
            httpx.Response(200, json={"result": {"price_usd": 1}}),  # settle #2: success
        ])
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        out = client.call_tool("token_price", {"symbol": "BTC"},
                               max_spend="0.05", prefer_chain="stacks",
                               chain_is_explicit=True)

    # Signed twice, against TWO DIFFERENT payment_ids — a fresh 402, not a reuse.
    assert wallet._seen == ["pid-A", "pid-B"]
    assert out is not None
