"""
test_paid_inputs.py — a paid call states its required inputs before it settles.

A paid call with an empty body used to settle and run on silent defaults. It
now gets a 400 naming what is missing, with the defaults ready to resend, and
no settle step runs. An unpaid call still gets the 402 challenge.
"""
import base64
import json

import pytest
from fastapi.responses import JSONResponse

from gateway.services import paid_inputs
from registry.registry import _TOOLS


def _sig(network: str) -> str:
    return base64.b64encode(json.dumps(
        {"x402Version": 2, "accepted": {"network": network}, "network": network,
         "payload": {}}).encode()).decode()


# One PAYMENT-SIGNATURE per rail that settles from a signed payload.
SIGNED_RAILS = {"base": _sig("eip155:8453"), "stacks": _sig("stacks:1"),
                "stellar-standard": _sig("stellar:pubnet")}
CLASSIC = {"X-Payment": "tx_hash=deadbeef,from=GABC,id=pay_x"}
SETTLERS = ("_settle_base_path", "_settle_stacks_path",
            "_settle_stellar_standard_path", "_settle_stellar")


@pytest.fixture
def no_settle(monkeypatch):
    """Any settle step fails the test."""
    import gateway.routes.tools as rt

    async def _boom(*a, **kw):
        raise AssertionError("a settle step ran")
    for name in SETTLERS:
        monkeypatch.setattr(rt, name, _boom)


@pytest.fixture
def settle_reached(monkeypatch):
    """Every settle step answers 418, so reaching one is visible."""
    import gateway.routes.tools as rt

    async def _teapot(*a, **kw):
        return JSONResponse(status_code=418, content={"settle": "reached"})
    for name in SETTLERS:
        monkeypatch.setattr(rt, name, _teapot)


class TestRules:
    def test_missing_and_alias(self):
        assert paid_inputs.missing("pre_trade_check", {}) == ["symbol"]
        assert paid_inputs.missing("pre_trade_check", {"symbol": "  "}) == ["symbol"]
        assert paid_inputs.missing("pre_trade_check", {"asset": "SOL"}) == []
        assert paid_inputs.missing("verified_route", None) == ["need"]
        assert paid_inputs.missing("session_create", {"max_spend": "0.05"}) == []

    def test_tools_without_a_rule_are_never_refused(self):
        assert paid_inputs.missing("funding_spread", {}) == []
        assert paid_inputs.refusal("sbtc_peg", {}) is None

    def test_suggestion_keeps_what_the_caller_sent(self):
        got = paid_inputs.suggested("pre_trade_check", {"side": "short", "size_usd": 500})
        assert got == {"symbol": "ETH", "side": "short", "size_usd": 500}

    def test_session_create_schema_requires_the_cap(self):
        assert _TOOLS["session_create"].parameters["required"] == ["max_spend"]


class TestPaidToolCalls:
    @pytest.mark.parametrize("rail", SIGNED_RAILS)
    @pytest.mark.parametrize("tool,gap", [("pre_trade_check", "symbol"),
                                          ("verified_route", "need")])
    def test_empty_paid_call_is_refused_before_any_settle(self, client, no_settle,
                                                          rail, tool, gap):
        r = client.post(f"/tools/{tool}/call",
                        headers={"PAYMENT-SIGNATURE": SIGNED_RAILS[rail]})
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error"] == "missing_required_parameters"
        assert body["missing"] == [gap] and body["charged"] is False
        assert gap in body["suggested_parameters"]

    @pytest.mark.parametrize("rail", SIGNED_RAILS)
    def test_resending_with_the_suggestion_reaches_settle(self, client, monkeypatch, rail):
        headers = {"PAYMENT-SIGNATURE": SIGNED_RAILS[rail]}
        import gateway.routes.tools as rt

        async def _boom(*a, **kw):
            raise AssertionError("a settle step ran")
        for name in SETTLERS:
            monkeypatch.setattr(rt, name, _boom)
        refused = client.post("/tools/pre_trade_check/call", headers=headers).json()

        async def _teapot(*a, **kw):
            return JSONResponse(status_code=418, content={"settle": "reached"})
        for name in SETTLERS:
            monkeypatch.setattr(rt, name, _teapot)
        r = client.post("/tools/pre_trade_check/call", headers=headers,
                        json={"parameters": refused["suggested_parameters"]})
        assert r.status_code == 418, r.text

    def test_asset_alias_is_accepted(self, client, settle_reached):
        r = client.post("/tools/pre_trade_check/call", json={"asset": "SOL"},
                        headers={"PAYMENT-SIGNATURE": SIGNED_RAILS["base"]})
        assert r.status_code == 418, r.text

    def test_classic_stellar_has_already_paid_so_it_is_not_refused(self, client,
                                                                   settle_reached):
        r = client.post("/tools/pre_trade_check/call", headers=CLASSIC)
        assert r.status_code == 418, r.text

    def test_paid_tools_without_a_rule_are_unchanged(self, client, settle_reached):
        r = client.post("/tools/funding_spread/call",
                        headers={"PAYMENT-SIGNATURE": SIGNED_RAILS["base"]})
        assert r.status_code == 418, r.text

    def test_free_tools_are_unchanged(self, client):
        r = client.post("/tools/token_price/call",
                        headers={"PAYMENT-SIGNATURE": SIGNED_RAILS["base"]})
        assert r.json().get("error") != "missing_required_parameters"


class TestUnpaidStillGetsThe402:
    @pytest.mark.parametrize("path,key", [
        ("/tools/pre_trade_check/call", "symbol"),
        ("/tools/verified_route/call", "need"),
        ("/v1/session/create", "max_spend")])
    def test_bare_post_is_a_402_that_carries_the_suggestion(self, client, path, key):
        r = client.post(path)
        assert r.status_code == 402, r.text
        body = r.json()
        assert body["required_parameters"] == [key]
        assert key in body["suggested_parameters"]

    def test_free_tool_402_has_no_suggestion(self, client):
        assert "required_parameters" not in client.post("/tools/token_price/call").json()


class TestSessionCreate:
    PATH = "/v1/session/create"

    @pytest.mark.parametrize("rail", SIGNED_RAILS)
    def test_no_cap_is_refused_before_settle(self, client, rail):
        r = client.post(self.PATH, json={"label": "x"},
                        headers={"PAYMENT-SIGNATURE": SIGNED_RAILS[rail]})
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["missing"] == ["max_spend"]
        assert body["suggested_parameters"] == {"max_spend": "0.10"}

    def test_with_a_cap_it_goes_on_to_settlement(self, client):
        r = client.post(self.PATH, json={"max_spend": "0.10"},
                        headers={"PAYMENT-SIGNATURE": SIGNED_RAILS["base"]})
        assert r.json().get("error") != "missing_required_parameters"

    def test_classic_stellar_keeps_the_default_cap(self, client):
        r = client.post(self.PATH, headers=CLASSIC)
        assert r.json().get("error") != "missing_required_parameters"
