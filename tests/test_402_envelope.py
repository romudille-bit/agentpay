"""
AGE-123 — the 402 JSON BODY must carry the resource-info block (and
extensions.bazaar) that previously lived ONLY inside the base64
PAYMENT-REQUIRED header.

Why this file exists: x402 trust validators (x402.fuchss.app) parse the 402
BODY. With the resource block header-only, every probe for 30 days was flagged
`envelope:missing-resource-info` (envelopeValid30d 0/793) → specComplianceScore
30 → grade C "avoid" → buyers steered to a competitor. This is the THIRD bug in
the header/body dialect-drift family (AGE-48: standard accepts[] header-only;
AGE-112: per-path envelope divergence), so beyond point assertions these tests
pin the structural invariant:

    body ⊇ header for the envelope core (resource, extensions.bazaar)

Both sides are built by the same function (gateway.base.build_resource_block),
so a drift can only reappear if someone bypasses the shared builder — which
these tests catch at the route level, not the builder level.
"""

import base64
import json

import pytest


def _decode_header(resp) -> dict:
    h = resp.headers.get("PAYMENT-REQUIRED")
    assert h, "PAYMENT-REQUIRED header missing"
    return json.loads(base64.b64decode(h + "=" * (-len(h) % 4)))


def _assert_body_superset_of_header_envelope(resp):
    """The structural invariant: the 402 body's envelope core (resource +
    extensions.bazaar) must equal the header's — same shared builder output."""
    body = resp.json()
    header = _decode_header(resp)

    assert "resource" in body, "402 body lacks the resource-info block (AGE-123)"
    assert body["resource"] == header["resource"], (
        "402 body resource block drifted from the PAYMENT-REQUIRED header — "
        "both must come from gateway.base.build_resource_block"
    )
    if "extensions" in header:
        assert body.get("extensions") == header["extensions"], (
            "402 body extensions drifted from the PAYMENT-REQUIRED header"
        )


class TestToolPath402BodyEnvelope:
    """POST + GET /tools/{name}/call — the fuchss-probed pre_trade_check path."""

    def test_paid_tool_post_body_has_resource_info(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post("/tools/pre_trade_check/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        res = r.json()["resource"]
        # The exact fields fuchss's `missing-resource-info` check keyed on.
        assert res["url"].endswith("/tools/pre_trade_check/call")
        assert res["mimeType"] == "application/json"
        assert res["description"]
        assert res["serviceName"] == "AgentPay Pre-Trade Risk Check"
        assert "pre-trade-check" in res["tags"]

    def test_paid_tool_post_body_superset_of_header(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post("/tools/pre_trade_check/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        _assert_body_superset_of_header_envelope(r)
        # extensions.bazaar mirrored into the body too (header parity)
        assert "bazaar" in r.json().get("extensions", {})

    def test_paid_tool_get_probe_body_superset_of_header(self, client, monkeypatch):
        """GET is what discovery/trust probes actually hit ~26×/day."""
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.get("/tools/pre_trade_check/call")
        assert r.status_code == 402
        _assert_body_superset_of_header_envelope(r)

    def test_verified_route_body_superset_of_header(self, client, monkeypatch):
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.get("/tools/verified_route/call")
        assert r.status_code == 402
        _assert_body_superset_of_header_envelope(r)
        assert r.json()["resource"]["serviceName"] == "AgentPay x402 Trust Oracle"

    def test_free_tool_body_has_resource_no_bazaar(self, client, monkeypatch):
        """Free tools have no Bazaar listing metadata, but their 402 envelope
        must still carry the basic resource-info block (url/description/
        mimeType) — envelope compliance isn't a paid-tool-only property."""
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post("/tools/token_price/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        body = r.json()
        res = body["resource"]
        assert res["url"].endswith("/tools/token_price/call")
        assert res["mimeType"] == "application/json"
        assert res["description"]
        # no bazaar entry for free tools → no extensions block (matches header)
        assert "extensions" not in body
        _assert_body_superset_of_header_envelope(r)

    def test_stellar_only_402_still_has_resource_info(self, client, monkeypatch):
        """With Base unconfigured there is no PAYMENT-REQUIRED header at all —
        the body is the ONLY envelope, so the resource block must be there."""
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "")

        r = client.post("/tools/pre_trade_check/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        assert "PAYMENT-REQUIRED" not in r.headers
        res = r.json()["resource"]
        assert res["url"].endswith("/tools/pre_trade_check/call")
        assert res["serviceName"] == "AgentPay Pre-Trade Risk Check"


class TestSessionPath402BodyEnvelope:
    """GET + POST /v1/session/create — the fuchss-probed session_create path."""

    def test_get_probe_body_has_resource_info(self, client, monkeypatch):
        import gateway.routes.session as sess
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.get("/v1/session/create")
        assert r.status_code == 402
        res = r.json()["resource"]
        assert res["url"] == sess.SESSION_RESOURCE_URL
        assert res["mimeType"] == "application/json"
        assert res["serviceName"] == "AgentPay Spend Cap & Receipts"
        assert "session" in res["tags"]

    def test_get_probe_body_superset_of_header(self, client, monkeypatch):
        import gateway.routes.session as sess
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.get("/v1/session/create")
        assert r.status_code == 402
        _assert_body_superset_of_header_envelope(r)
        assert "bazaar" in r.json().get("extensions", {})

    def test_post_challenge_body_superset_of_header(self, client, monkeypatch):
        import gateway.routes.session as sess
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.post("/v1/session/create", json={"max_spend": "0.10"})
        assert r.status_code == 402
        _assert_body_superset_of_header_envelope(r)

    def test_tool_path_and_session_path_declare_same_resource(
            self, client, monkeypatch):
        """AGE-112 companion: both payable paths for session_create must expose
        the SAME canonical resource block in the body (not just the header)."""
        import gateway.routes.tools as rt
        import gateway.routes.session as sess
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r_tool = client.post("/tools/session_create/call",
                             json={"parameters": {"max_spend": "0.10"}})
        r_sess = client.get("/v1/session/create")
        assert r_tool.status_code == r_sess.status_code == 402
        assert r_tool.json()["resource"] == r_sess.json()["resource"]

    def test_stellar_only_402_still_has_resource_info(self, client, monkeypatch):
        import gateway.routes.session as sess
        monkeypatch.setattr(sess.settings, "BASE_GATEWAY_ADDRESS", "")

        r = client.get("/v1/session/create")
        assert r.status_code == 402
        assert "PAYMENT-REQUIRED" not in r.headers
        res = r.json()["resource"]
        assert res["url"] == sess.SESSION_RESOURCE_URL
        assert res["serviceName"] == "AgentPay Spend Cap & Receipts"


class TestSharedBuilder:
    """The builder itself: header and body callers get identical output."""

    def test_build_resource_block_matches_header_resource(self):
        from gateway import base as base_pay

        bazaar_resource = {
            "url":         "https://agentpay.tools/v1/session/create",
            "serviceName": "AgentPay Spend Cap & Receipts",
            "tags":        ["budget", "receipts"],
            "description": "richer bazaar description",
        }
        req = {"scheme": "exact", "network": "eip155:8453", "amount": "10000",
               "asset": "0x" + "a" * 40, "payTo": "0x" + "b" * 40,
               "maxTimeoutSeconds": 300}

        block = base_pay.build_resource_block(
            "https://agentpay.tools/tools/x/call", "plain description",
            bazaar_resource,
        )
        header = base_pay.build_payment_required_header(
            requirements=req,
            resource_url="https://agentpay.tools/tools/x/call",
            tool_description="plain description",
            bazaar_resource=bazaar_resource,
        )
        payload = json.loads(base64.b64decode(header + "=" * (-len(header) % 4)))
        assert payload["resource"] == block
        # canonical-URL override (AGE-112) applied identically on both sides
        assert block["url"] == "https://agentpay.tools/v1/session/create"

    def test_build_resource_block_minimal(self):
        from gateway import base as base_pay
        block = base_pay.build_resource_block("https://x/y", "desc")
        assert block == {"url": "https://x/y", "description": "desc",
                         "mimeType": "application/json"}
