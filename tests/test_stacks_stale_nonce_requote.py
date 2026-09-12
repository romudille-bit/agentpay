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
from agentpay._wallet import BudgetExceeded

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
    w._stacks_api_base = "https://api.testnet.hiro.so"
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
        # AGE-152: the SDK asks Hiro before trusting "rejected"; the honest
        # case is a txid the chain has never seen.
        respx.get(url__regex=r".*/extended/v1/tx/0x.*").mock(
            return_value=httpx.Response(404))
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


def test_overcap_requote_refused():
    """A fresh 402 that re-quotes above the cap is refused before signing —
    the cap binds the retry quote, not just the original one."""
    import pytest
    wallet = _stacks_wallet()
    overcap = _402("pid-B")
    overcap["amount_usdc"] = "0.50"
    overcap["payment_options"]["stacks"]["amount_usdc"] = "0.50"
    with respx.mock:
        # AGE-152: the SDK asks Hiro before trusting "rejected"; the honest
        # case is a txid the chain has never seen.
        respx.get(url__regex=r".*/extended/v1/tx/0x.*").mock(
            return_value=httpx.Response(404))
        respx.post(TOOL_URL).mock(side_effect=[
            httpx.Response(402, json=_402("pid-A")),
            httpx.Response(409, json={"payment_status": "rejected",
                                      "error_reason": "broadcast rejected: bad nonce"}),
            httpx.Response(402, json=overcap),
        ])
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with pytest.raises(BudgetExceeded):
            client.call_tool("token_price", {"symbol": "BTC"},
                             max_spend="0.05", prefer_chain="stacks",
                             chain_is_explicit=True)
    # Only the first quote was ever signed.
    assert wallet._seen == ["pid-A"]


def test_requote_to_another_recipient_is_refused_by_the_rules():
    """The spending rules bind the retry quote too, not only the cap.

    A fresh 402 is a different offer: it can name another recipient and sit on
    the other side of the approval threshold. One fabricated "bad nonce" reply
    is all it takes to ask for one, so the rules run again before signing.
    """
    import pytest
    from agentpay._wallet import Session, PolicyRejected

    wallet = _stacks_wallet()
    elsewhere = _402("pid-B")
    elsewhere["pay_to"] = "ST2ELSEWHERE00000000000000000000000000000"
    elsewhere["payment_options"]["stacks"]["pay_to"] = (
        "ST2ELSEWHERE00000000000000000000000000000")

    session = Session(wallet=wallet, gateway_url=GATEWAY, max_spend="1.00",
                      allowed_recipients=[PAY_TO], prefer_chain="stacks")

    with respx.mock:
        respx.get(url__regex=r".*/extended/v1/tx/0x.*").mock(
            return_value=httpx.Response(404))
        respx.post(TOOL_URL).mock(side_effect=[
            httpx.Response(402, json=_402("pid-A")),
            httpx.Response(409, json={"payment_status": "rejected",
                                      "error_reason": "broadcast rejected: bad nonce"}),
            httpx.Response(402, json=elsewhere),
        ])
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with pytest.raises(PolicyRejected):
            client.call_tool("token_price", {"symbol": "BTC"},
                             max_spend="0.05", prefer_chain="stacks",
                             chain_is_explicit=True,
                             pre_pay_check=session._pre_pay_check)

    # The allowlisted quote was signed; the substituted one never was.
    assert wallet._seen == ["pid-A"]


def test_requote_above_the_approval_threshold_asks_again():
    """An amount over approve_above needs approval on the retry quote as well."""
    import pytest
    from agentpay._wallet import Session, ApprovalRequired

    wallet = _stacks_wallet()
    dearer = _402("pid-B")
    dearer["amount_usdc"] = "0.02"
    dearer["payment_options"]["stacks"]["amount_usdc"] = "0.02"
    asked = []

    def approver(request):
        asked.append(request)
        return False

    session = Session(wallet=wallet, gateway_url=GATEWAY, max_spend="1.00",
                      approve_above="0.015", approver=approver,
                      prefer_chain="stacks")

    with respx.mock:
        respx.get(url__regex=r".*/extended/v1/tx/0x.*").mock(
            return_value=httpx.Response(404))
        respx.post(TOOL_URL).mock(side_effect=[
            httpx.Response(402, json=_402("pid-A")),
            httpx.Response(409, json={"payment_status": "rejected",
                                      "error_reason": "broadcast rejected: bad nonce"}),
            httpx.Response(402, json=dearer),
        ])
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with pytest.raises(ApprovalRequired):
            client.call_tool("token_price", {"symbol": "BTC"},
                             max_spend="0.05", prefer_chain="stacks",
                             chain_is_explicit=True,
                             pre_pay_check=session._pre_pay_check)

    assert wallet._seen == ["pid-A"]
    assert asked, "the approver was never consulted on the re-quote"
