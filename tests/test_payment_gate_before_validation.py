"""
AGE-134 — the 402 challenge must come BEFORE request-body validation.

Why this file exists: FastAPI's declarative body binding (`body: Model`)
validated the request body before the route handler ran. An unpaid caller
that sent a bare POST (no body), an empty body, or a non-JSON content-type
got a 422 instead of the 402 challenge — and HEAD on /v1/session/create got
a 405 (FastAPI does not auto-answer HEAD for GET routes). Paying clients and
internal monitoring both send well-formed JSON, so this was invisible to us
while any third-party prober sending bodyless POSTs recorded a healthy
gateway as "not returning 402" — the exact failure CDP's curation bar
delists for (≥99% platform-measured availability; "endpoints that stop
returning 402 ... are eventually removed from the index").

The invariant these tests pin:

    an unpaid request to a paid resource returns 402, ALWAYS,
    regardless of method quirk, body shape, or content-type;
    body validation happens only on the paid path, BEFORE settlement.

Reproduced against production 2026-08-18 (all three indexed resources):
bare POST → 422, empty body + json CT → 422, {} with form CT → 422,
HEAD /v1/session/create → 405, HEAD /tools/{name}/call → 200 (not 402).
"""

import pytest


# The three indexed resources probed in AGE-134, minus session (own path):
PAID_TOOL_PATHS = [
    "/tools/pre_trade_check/call",
    "/tools/verified_route/call",
]
SESSION_PATH = "/v1/session/create"
FREE_TOOL_PATH = "/tools/token_price/call"

ALL_402_PATHS = [SESSION_PATH, *PAID_TOOL_PATHS, FREE_TOOL_PATH]

# Every body shape a prober (or a sloppy client) might send WITHOUT payment
# headers. All of them must get the 402 challenge, never a 422.
BODY_SHAPES = [
    # (label, content bytes or None, extra headers)
    ("bare_post_no_body_no_ct",  None,          {}),
    ("empty_body_json_ct",       b"",           {"Content-Type": "application/json"}),
    ("empty_json_form_ct",       b"{}",         {"Content-Type": "application/x-www-form-urlencoded"}),
    ("empty_json_text_ct",       b"{}",         {"Content-Type": "text/plain"}),
    ("invalid_json_json_ct",     b"not json {", {"Content-Type": "application/json"}),
    ("non_dict_json",            b'"a string"', {"Content-Type": "application/json"}),
    ("model_invalid_parameters", b'{"parameters": "not-a-dict"}',
                                 {"Content-Type": "application/json"}),
]


class TestUnpaidAlways402:
    """No payment header → 402, no matter what the body looks like."""

    @pytest.mark.parametrize("path", ALL_402_PATHS)
    @pytest.mark.parametrize("label,content,headers",
                             BODY_SHAPES, ids=[s[0] for s in BODY_SHAPES])
    def test_post_any_body_shape_gets_402(self, client, path, label,
                                          content, headers):
        kwargs = {"headers": headers} if headers else {}
        if content is not None:
            kwargs["content"] = content
        r = client.post(path, **kwargs)
        assert r.status_code == 402, (
            f"{label} POST {path} → {r.status_code} (expected 402): {r.text}"
        )

    @pytest.mark.parametrize("path", ALL_402_PATHS)
    def test_bare_post_402_body_is_a_real_challenge(self, client, path):
        """The lenient path must produce the same envelope as a well-formed
        402 — resource-info block included (the AGE-123 lesson: validators
        parse the body)."""
        r = client.post(path)
        assert r.status_code == 402
        body = r.json()
        assert "resource" in body, "bare-POST 402 lacks the resource block"
        assert "payment_id" in body or body.get("accepts") is not None

    @pytest.mark.parametrize("path", ALL_402_PATHS)
    def test_head_returns_402_empty_body(self, client, path):
        r = client.head(path)
        assert r.status_code == 402, (
            f"HEAD {path} → {r.status_code} (expected 402)"
        )
        assert r.content in (b"", None) or len(r.content) == 0

    @pytest.mark.parametrize("path", ALL_402_PATHS)
    def test_get_probe_still_402(self, client, path):
        r = client.get(path)
        assert r.status_code == 402


class TestNotFoundStillWins:
    """The 404 for an unknown tool outranks the payment gate (a missing
    resource is not a paid resource)."""

    def test_bare_post_unknown_tool_404(self, client):
        r = client.post("/tools/nonexistent_tool/call")
        assert r.status_code == 404

    def test_head_unknown_tool_404(self, client):
        r = client.head("/tools/nonexistent_tool/call")
        assert r.status_code == 404


class TestPaidPathStillValidates:
    """With a payment header present, a malformed body must 422 BEFORE any
    settlement is attempted — a payer must not burn a payment on a call the
    gateway cannot execute, and a garbled body must not silently execute
    with defaults."""

    PAYMENT_HEADER = {"X-Payment": "tx_hash=deadbeef,from=GABC,id=pay_x"}

    def test_tools_invalid_json_with_payment_header_422(self, client):
        r = client.post(
            "/tools/pre_trade_check/call",
            content=b"not json {",
            headers={"Content-Type": "application/json", **self.PAYMENT_HEADER},
        )
        assert r.status_code == 422
        assert r.json()["detail"][0]["type"] == "json_invalid"

    def test_tools_model_invalid_with_payment_header_422(self, client):
        r = client.post(
            "/tools/pre_trade_check/call",
            json={"parameters": "not-a-dict"},
            headers=self.PAYMENT_HEADER,
        )
        assert r.status_code == 422

    def test_session_model_invalid_with_payment_header_422(self, client):
        r = client.post(
            SESSION_PATH,
            json={"max_spend": {"not": "a-string"}},
            headers=self.PAYMENT_HEADER,
        )
        assert r.status_code == 422

    def test_paid_empty_body_accepted_as_defaults(self, client):
        """A paid retry with an empty body is a legitimate call for the
        tool's default behaviour — it must reach settlement (which then
        fails on the fake tx, NOT on body validation)."""
        r = client.post("/tools/pre_trade_check/call",
                        headers=self.PAYMENT_HEADER)
        assert r.status_code != 422, r.text


class TestWellFormedCallersUnchanged:
    """The canonical shapes must be byte-for-byte unaffected."""

    def test_nested_parameters_still_402_with_challenge(self, client):
        r = client.post("/tools/pre_trade_check/call",
                        json={"parameters": {"symbol": "ETH"}})
        assert r.status_code == 402
        assert "payment_id" in r.json()

    def test_session_max_spend_still_flows_into_challenge(self, client):
        r = client.post(SESSION_PATH, json={"max_spend": "0.25"})
        assert r.status_code == 402
