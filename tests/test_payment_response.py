"""PAYMENT-RESPONSE: the settlement receipt header on a paid 200."""
import base64
import json

import pytest

from gateway import payment_response as pr


def _decode(value: str) -> dict:
    return json.loads(base64.b64decode(value))


def test_caip2_mapping():
    assert pr.caip2_of("base-mainnet") == "eip155:8453"
    assert pr.caip2_of("base") == "eip155:8453"
    assert pr.caip2_of("base-sepolia") == "eip155:84532"
    assert pr.caip2_of("stellar-mainnet").startswith("stellar:")
    assert pr.caip2_of("stacks-mainnet").startswith("stacks:")
    assert pr.caip2_of("eip155:8453") == "eip155:8453"      # already CAIP-2
    assert pr.caip2_of("eip155:1234") == "eip155:1234"      # unknown passes through


def test_headers_for_paid_stellar_settle():
    auth = {"authorized": True, "tx_hash": "6d23" * 16, "payer": "G" + "A" * 55,
            "network": "stellar-mainnet"}
    h = pr.headers_for(auth)
    assert set(h) == {"PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"}
    assert h["PAYMENT-RESPONSE"] == h["X-PAYMENT-RESPONSE"]
    body = _decode(h["PAYMENT-RESPONSE"])
    assert body == {"success": True, "transaction": "6d23" * 16,
                    "network": pr.caip2_of("stellar-mainnet"), "payer": "G" + "A" * 55}


def test_no_header_without_a_transaction():
    assert pr.headers_for({"authorized": True, "tx_hash": "", "payer": "x",
                           "network": "base"}) == {}
    assert pr.headers_for({"authorized": False, "tx_hash": "0xabc"}) == {}
    assert pr.headers_for({}) == {}


# ── Route contract: the header rides on a paid 200, never on a $0 call ───────

AGENT = "GAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGENTAGEN"


@pytest.fixture
def _stellar_classic(monkeypatch):
    """Classic Stellar path with the on-chain check and the upstream mocked.
    verify_and_fulfill itself runs, so its real return shape (no payer,
    no network) is what the route has to cope with."""
    import gateway.x402 as x402_mod
    import gateway.routes.tools as rt

    async def verify(**kw):
        return {"verified": True, "reason": "ok"}

    async def split(**kw):
        return {"success": True}

    async def upstream(tool_name, params):
        return {"tool": tool_name, "mocked": True}

    monkeypatch.setattr(x402_mod, "verify_payment", verify)
    monkeypatch.setattr(x402_mod, "split_payment", split)
    monkeypatch.setattr(rt, "real_tool_response", upstream)


def _pay(client, tool, tx):
    ch = client.post(f"/tools/{tool}/call",
                     json={"parameters": {"symbol": "ETH"}, "agent_address": AGENT}).json()
    return client.post(f"/tools/{tool}/call",
                       json={"parameters": {"symbol": "ETH"}, "agent_address": AGENT},
                       headers={"X-Payment": f"tx_hash={tx},from={AGENT},id={ch['payment_id']}"})


def test_paid_stellar_call_carries_the_receipt_header(client, _stellar_classic):
    r = _pay(client, "pre_trade_check", "ab" * 32)
    assert r.status_code == 200, r.text
    assert r.headers["PAYMENT-RESPONSE"] == r.headers["X-PAYMENT-RESPONSE"]
    body = _decode(r.headers["PAYMENT-RESPONSE"])
    assert body["success"] is True
    assert body["transaction"] == "ab" * 32 == r.json()["payment"]["tx_hash"]
    assert body["network"].startswith("stellar:")
    assert body["payer"] == AGENT


def test_free_call_has_no_receipt_header(client, _stellar_classic):
    r = _pay(client, "token_price", "free:abc")
    assert r.status_code == 200, r.text
    assert "PAYMENT-RESPONSE" not in r.headers
    assert "X-PAYMENT-RESPONSE" not in r.headers


def test_session_create_carries_the_receipt_header(client, _stellar_classic):
    ch = client.post("/v1/session/create", json={"agent_address": AGENT}).json()
    r = client.post("/v1/session/create",
                    json={"agent_address": AGENT, "max_spend": "0.20"},
                    headers={"X-Payment": f"tx_hash={'cd' * 32},from={AGENT},id={ch['payment_id']}"})
    assert r.status_code == 200, r.text
    body = _decode(r.headers["PAYMENT-RESPONSE"])
    assert body["transaction"] == "cd" * 32 and body["payer"] == AGENT
    assert body["network"].startswith("stellar:")


def test_receipt_header_is_exposed_to_browsers(client):
    # CORS only decorates responses to cross-origin requests.
    r = client.post("/tools/pre_trade_check/call", json={"parameters": {}},
                    headers={"Origin": "https://example.com"})
    exposed = r.headers.get("access-control-expose-headers", "")
    assert "PAYMENT-RESPONSE" in exposed and "X-PAYMENT-RESPONSE" in exposed
