"""
AGE-129 — documented error responses in the agent-ready metadata.

CDP's Bazaar curation requirements list "complete input schema … per-call
pricing, supported networks, documented error responses" as the agent-ready
bar. Before this change our metadata carried input schema + output example
but zero error documentation — the one named requirement we plainly failed.

These tests pin that every curated resource's live 402 documents its error
responses in BOTH places an agent reads:

  1. the PAYMENT-REQUIRED header's accepts[0].outputSchema.errors
  2. the body's extensions.bazaar.info.errors (mirrored header/body, AGE-123)

and that the catalogue itself covers the statuses the gateway actually
returns (402/422/404/429/502/503 — see gateway.base.build_error_responses).
"""

import base64
import json

import pytest

from gateway.base import build_error_responses

CURATED = [
    "/v1/session/create",
    "/tools/pre_trade_check/call",
    "/tools/verified_route/call",
]


def _decode_header(resp) -> dict:
    h = resp.headers.get("PAYMENT-REQUIRED")
    assert h, "PAYMENT-REQUIRED header missing"
    return json.loads(base64.b64decode(h + "=" * (-len(h) % 4)))


class TestErrorCatalogue:

    def test_covers_the_statuses_the_gateway_returns(self):
        statuses = {e["status"] for e in build_error_responses()}
        assert statuses == {402, 422, 404, 429, 502, 503}

    def test_every_entry_has_when_and_body(self):
        for e in build_error_responses():
            assert e.get("when") or e.get("body"), e
            assert isinstance(e["status"], int)

    def test_returns_fresh_copy(self):
        a = build_error_responses()
        a[0]["status"] = 999
        assert build_error_responses()[0]["status"] == 402


@pytest.mark.usefixtures("client")
class TestCuratedResourcesDocumentErrors:

    @pytest.mark.parametrize("path", CURATED)
    def test_header_output_schema_carries_errors(self, client, monkeypatch, path):
        # Base must be configured or the PAYMENT-REQUIRED header is absent.
        import gateway.routes.tools as rt
        import gateway.routes.session as rs
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        monkeypatch.setattr(rs.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.get(path)
        assert r.status_code == 402
        header = _decode_header(r)
        schema = header["accepts"][0].get("outputSchema") or {}
        errors = schema.get("errors")
        assert errors, f"{path}: outputSchema.errors missing from PAYMENT-REQUIRED"
        assert {e["status"] for e in errors} == {402, 422, 404, 429, 502, 503}

    @pytest.mark.parametrize("path", CURATED)
    def test_body_bazaar_info_carries_errors(self, client, monkeypatch, path):
        import gateway.routes.tools as rt
        import gateway.routes.session as rs
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        monkeypatch.setattr(rs.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)

        r = client.get(path)
        assert r.status_code == 402
        info = (r.json().get("extensions") or {}).get("bazaar", {}).get("info", {})
        errors = info.get("errors")
        assert errors, f"{path}: extensions.bazaar.info.errors missing from 402 body"
        assert {e["status"] for e in errors} == {402, 422, 404, 429, 502, 503}

    def test_header_stays_a_sane_size(self, client, monkeypatch):
        """The catalogue rides inside the base64 PAYMENT-REQUIRED header on
        every 402 — keep the whole header comfortably under proxy limits."""
        import gateway.routes.tools as rt
        monkeypatch.setattr(rt.settings, "BASE_GATEWAY_ADDRESS", "0x" + "c" * 40)
        r = client.get("/tools/pre_trade_check/call")
        assert len(r.headers["PAYMENT-REQUIRED"]) < 16_000
