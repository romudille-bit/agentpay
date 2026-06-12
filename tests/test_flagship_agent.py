"""
test_flagship_agent.py — the flagship analyst's pure functions.

The run loop talks to production and is exercised by the daily cron; the
note composition and regime logic are pinned here.
"""

from agents.analyst.run import compose_note, regime_line


class TestRegimeLine:

    def test_full_data(self):
        line = regime_line(
            {"value": 23, "value_classification": "Extreme Fear"},
            {"rates": [{"sentiment": "bearish"}, {"sentiment": "bearish"},
                       {"sentiment": "neutral"}]},
        )
        assert "Fear & Greed 23 (Extreme Fear)" in line
        assert "crowded-long" in line

    def test_missing_everything(self):
        assert regime_line(None, None) == "regime data unavailable"

    def test_balanced_funding(self):
        line = regime_line(None, {"rates": [{"sentiment": "neutral"}]})
        assert "unremarkable" in line


class TestComposeNote:

    def test_verdicts_and_skips_render(self):
        note = compose_note(
            "2026-06-12 13:00 UTC",
            "Fear & Greed 50 (Neutral)",
            {
                "BTC": {"verdict": "ok", "factors": {
                    "liquidity": {"level": "ok", "reason": "fine"}}},
                "ETH": {"verdict": "caution", "factors": {
                    "carry": {"level": "caution",
                              "reason": "longs paying elevated funding"}}},
            },
            skipped={"SOL": "budget cap reached"},
        )
        assert "BTC: OK" in note
        assert "ETH: CAUTION (carry: longs paying elevated funding)" in note
        assert "SOL: skipped (budget cap reached)" in note
        assert "Regime: Fear & Greed 50 (Neutral)" in note

    def test_empty_verdicts_still_render_header(self):
        note = compose_note("2026-06-12", "regime data unavailable", {}, {})
        assert "flagship analyst" in note
