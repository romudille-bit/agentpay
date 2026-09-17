"""The .well-known domain-proof files directories read to let us claim a listing."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes import discovery


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(discovery.router)
    return TestClient(app)


def test_x402_trust_proof_is_one_fixed_line(monkeypatch):
    """The verifier looks for `x402-trust-verification=v1:<key>`; the body
    must be exactly that, never anything a caller could steer (the same
    file can also carry a delist directive)."""
    monkeypatch.setattr(discovery.settings, "X402_TRUST_PROVIDER_KEY", "MFkwEwYHKoZI")
    r = _client().get("/.well-known/x402-trust.txt?x=x402-trust-remove")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "x402-trust-verification=v1:MFkwEwYHKoZI\n"
    assert "remove" not in r.text


def test_x402_trust_key_is_base64url_unpadded():
    """Their verifier compares the published line byte-for-byte against the
    key registered in the claim, which it renders base64url without padding;
    a standard-base64 key ('/' or trailing '=') would never match."""
    from gateway.config import settings
    key = settings.X402_TRUST_PROVIDER_KEY
    assert key and "/" not in key and "+" not in key and not key.endswith("=")


def test_x402_trust_proof_is_absent_when_unconfigured(monkeypatch):
    monkeypatch.setattr(discovery.settings, "X402_TRUST_PROVIDER_KEY", "")
    assert _client().get("/.well-known/x402-trust.txt").status_code == 404
