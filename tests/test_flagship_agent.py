"""
test_flagship_agent.py — the flagship analyst's pure functions.

The run loop talks to production and is exercised by the daily cron; the
note composition and regime logic are pinned here.
"""

from agents.analyst.run import (
    compose_note,
    context_line,
    news_summary,
    regime_line,
    _human_usd,
    _tvl_total,
)


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

    def test_context_line_renders_when_present(self):
        note = compose_note(
            "2026-06-12 13:00 UTC", "Fear & Greed 50 (Neutral)",
            {}, {}, context="12 headlines (net bullish); ETH gas 2.0 gwei",
        )
        assert "Context: 12 headlines (net bullish); ETH gas 2.0 gwei" in note

    def test_context_omitted_when_blank(self):
        note = compose_note("2026-06-12", "regime", {}, {}, context="")
        assert "Context:" not in note


class TestFreeIntelHelpers:

    def test_news_summary_net_sentiment_and_top(self):
        ns = news_summary({"headlines": [
            {"sentiment": "bullish", "score": 10, "title": "low"},
            {"sentiment": "bullish", "score": 99, "title": "TOP"},
            {"sentiment": "bearish", "score": 5, "title": "x"},
        ]})
        assert ns["count"] == 3
        assert ns["net_sentiment"] == "bullish"
        assert ns["top_headline"] == "TOP"

    def test_news_summary_empty(self):
        assert news_summary({"headlines": []}) is None
        assert news_summary(None) is None

    def test_tvl_total_shapes(self):
        assert _tvl_total({"tvl": 23_800_000_000.0}) == 23_800_000_000.0
        assert _tvl_total({"protocols": [{"tvl": 100}, {"tvl": 50}]}) == 150
        assert _tvl_total([{"tvl": 10}, {"tvl": 5}]) == 15
        assert _tvl_total(None) is None

    def test_human_usd(self):
        assert _human_usd(23_800_000_000) == "$23.8B"
        assert _human_usd(1_500_000) == "$1.5M"
        assert _human_usd(None) is None

    def test_context_line_defensive(self):
        line = context_line(
            {"headlines": [{"sentiment": "bullish", "score": 1, "title": "a"}]},
            {"standard_gwei": 2.0},
            {"tvl": 50_000_000_000.0, "change_1d": 1.2},
        )
        assert "1 headlines (net bullish)" in line
        assert "ETH gas 2.0 gwei" in line
        assert "DeFi TVL $50.0B (+1.2% 24h)" in line

    def test_context_line_all_missing(self):
        assert context_line(None, None, None) == ""
