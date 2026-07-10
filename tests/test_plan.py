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


class TestExternalSteps:
    """AGE-8: external x402 URLs annotated (and priced) from Prober telemetry."""

    URL = "https://api.ext.example/tools/search"

    def _scores(self, **over):
        row = {"resource_url": self.URL, "window_days": 30, "paid_probes": 4,
               "delivery_rate": 1.0, "delivery_factor": 1.15,
               "latency_p50_ms": 700, "flags": [], "mpp_option": True,
               "price_usdc": "0.02", "last_fail_at": None}
        row.update(over)
        return {self.URL: row}

    def _patch(self, monkeypatch, scores):
        from gateway.services import supabase

        async def _fake():
            return scores
        monkeypatch.setattr(supabase, "fetch_service_scores", _fake)

    def test_probed_external_step_priced_and_labeled(self, client, monkeypatch):
        self._patch(monkeypatch, self._scores())
        r = client.post("/v1/plan/estimate", json={
            "steps": [{"tool": "token_price"}, {"tool": self.URL}],
            "budget": "0.05",
        })
        assert r.status_code == 200
        body = r.json()
        step = body["steps"][1]
        assert step["external"] is True and step["probed"] is True
        assert step["mpp_option"] is True                 # [MR-3] label
        assert step["price_usdc"] == "0.02"
        assert step["price_source"] == "prober_last_seen_402"
        assert "probed 4×" in step["why"]
        assert "also payable via MPP/Tempo" in step["why"]
        from decimal import Decimal
        assert Decimal(body["total_usdc"]) == Decimal("0.02")   # external leg priced
        assert body["fits_budget"] is True

    def test_unprobed_external_step_annotated_not_priced(self, client, monkeypatch):
        self._patch(monkeypatch, {})
        r = client.post("/v1/plan/estimate", json={"steps": [{"tool": self.URL}]})
        step = r.json()["steps"][0]
        assert step["external"] is True and step["probed"] is False
        assert "price_usdc" not in step
        assert r.json()["total_usdc"] == "0"

    def test_registry_only_plan_never_fetches_scores(self, client, monkeypatch):
        from gateway.services import supabase

        async def _boom():
            raise AssertionError("fetch_service_scores must not be called")
        monkeypatch.setattr(supabase, "fetch_service_scores", _boom)
        r = client.post("/v1/plan/estimate", json={"steps": [{"tool": "token_price"}]})
        assert r.status_code == 200

    def test_mpp_only_metadata_without_paid_probes(self, client, monkeypatch):
        # T0-only knowledge: no paid probes yet, but MPP label + price known.
        self._patch(monkeypatch, self._scores(paid_probes=0, delivery_rate=None,
                                              delivery_factor=1.0))
        r = client.post("/v1/plan/estimate", json={"steps": [{"tool": self.URL}]})
        step = r.json()["steps"][0]
        assert step["mpp_option"] is True
        assert step["why"] == "also payable via MPP/Tempo"
