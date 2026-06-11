"""
test_routes_agent.py — Tests for POST /v1/agent/register.

Covers the three network modes (stellar default, base, both) and the
response contract the SDK quickstart() depends on.
"""

import pytest


class TestRegisterAgent:

    def test_default_mints_stellar(self, client):
        resp = client.post("/v1/agent/register", json={})
        assert resp.status_code == 200
        body = resp.json()
        w = body["wallet"]
        assert w["network"] == "stellar"
        assert w["public_key"].startswith("G")
        assert w["secret_key"].startswith("S")
        assert w["funded"] is False
        assert body["session_token"]
        assert isinstance(body["free_tools"], list)
        assert "wallets" not in body  # only present for network="both"

    def test_base_mints_evm(self, client):
        resp = client.post("/v1/agent/register", json={"network": "base"})
        assert resp.status_code == 200
        w = resp.json()["wallet"]
        assert w["network"] == "base"
        assert w["public_key"].startswith("0x") and len(w["public_key"]) == 42
        assert w["secret_key"].startswith("0x")

    def test_both_mints_stellar_and_base(self, client):
        resp = client.post("/v1/agent/register", json={"network": "both"})
        assert resp.status_code == 200
        body = resp.json()
        # Primary stays Stellar for back-compat
        assert body["wallet"]["network"] == "stellar"
        wallets = body["wallets"]
        assert wallets["stellar"]["public_key"].startswith("G")
        assert wallets["base"]["public_key"].startswith("0x")
        assert len(wallets["base"]["public_key"]) == 42

    def test_wallets_are_unique_per_register(self, client):
        a = client.post("/v1/agent/register", json={"network": "both"}).json()
        b = client.post("/v1/agent/register", json={"network": "both"}).json()
        assert a["wallet"]["public_key"] != b["wallet"]["public_key"]
        assert a["wallets"]["base"]["public_key"] != b["wallets"]["base"]["public_key"]
        assert a["agent_id"] != b["agent_id"]
