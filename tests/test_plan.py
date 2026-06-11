"""
test_plan.py — POST /v1/plan/estimate (pre-flight plan cost).

Pins: per-step pricing, free/paid counting, fits-budget verdicts,
unknown tools as non-fatal, cheaper-alternative suggestions, and the
legacy alias resolution.
"""

from decimal import Decimal


class TestEstimatePlan:

    def test_all_free_plan(self, client):
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "token_price"}, {"tool": "gas_tracker"}],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["total_usdc"] == "0"
        assert body["free_calls"] == 2
        assert body["paid_calls"] == 0
        assert all(s["free"] for s in body["steps"])

    def test_mixed_plan_totals_and_budget_verdict(self, client):
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "token_price"}, {"tool": "session_create"}],
            "budget": "0.05",
        })
        body = r.json()
        assert Decimal(body["total_usdc"]) == Decimal("0.01")
        assert body["paid_calls"] == 1
        assert body["fits_budget"] is True
        assert Decimal(body["remaining_after"]) == Decimal("0.04")

    def test_over_budget_verdict(self, client):
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "session_create"}],
            "budget": "0.001",
        })
        body = r.json()
        assert body["fits_budget"] is False
        assert body["remaining_after"] is None

    def test_unknown_tool_is_non_fatal(self, client):
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "nope_not_real"}, {"tool": "token_price"}],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["unknown_tools"] == 1
        assert body["steps"][0]["exists"] is False
        assert body["steps"][1]["exists"] is True
        assert body["total_usdc"] == "0"  # unknown tools don't price

    def test_paid_step_gets_cheaper_alternative(self, client):
        # session_create is the only paid tool; the cheapest same-category
        # alternative (if any exists) must be strictly cheaper.
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "session_create"}],
        })
        step = r.json()["steps"][0]
        if "cheaper_alternative" in step:
            assert Decimal(step["cheaper_alternative"]["price_usdc"]) < Decimal(step["price_usdc"])

    def test_legacy_alias_resolves(self, client):
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "dex_liquidity"}],
        })
        step = r.json()["steps"][0]
        assert step["exists"] is True
        assert step["tool"] == "token_market_data"

    def test_no_budget_means_no_verdict(self, client):
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "token_price"}],
        })
        body = r.json()
        assert "fits_budget" not in body

    def test_bad_budget_reported_not_fatal(self, client):
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "token_price"}],
            "budget": "lots",
        })
        assert r.status_code == 200
        assert "budget_error" in r.json()
