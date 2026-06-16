"""
test_flagship_agent.py — the flagship analyst's pure functions.

The run loop talks to production and is exercised by the daily cron; the
note composition and regime logic are pinned here.
"""

from agents.analyst.run import (
    build_findings,
    compact_verdict,
    compose_note,
    context_line,
    news_summary,
    regime_line,
    select_goal,
    _funding_bias,
    _human_usd,
    _tvl_total,
)


class TestGoalRotation:

    def test_rotation_cycles_five_goals(self):
        names = [select_goal(d)["name"] for d in range(10)]
        # 5-goal cycle repeats (verified_route added at index 2)
        assert names[:5] == ["pretrade_majors", "regime_brief", "verified_route",
                             "pretrade_alts", "crowding_watch"]
        assert names[5:] == names[:5]

    def test_paid_and_free_mix(self):
        majors = select_goal(0)
        regime = select_goal(1)
        assert majors["kind"] == "pre_trade" and majors["paid_symbols"] == ["BTC", "ETH"]
        assert regime["kind"] == "regime" and regime["paid_symbols"] == []

    def test_verified_route_is_in_rotation(self):
        vr = select_goal(2)   # verified_route at index 2
        assert vr["name"] == "verified_route" and vr["kind"] == "vetting"
        assert vr["paid_symbols"] == []       # the one paid leg is the verified_route call itself
        assert vr["vr_need"]                  # carries a need to vet
        assert vr["objective"]["kind"] == "vetting"

    def test_force_overrides_rotation(self):
        assert select_goal(0, force="crowding_watch")["name"] == "crowding_watch"

    def test_symbols_override_on_pretrade(self):
        g = select_goal(0, symbols_override=["SOL"])
        assert g["paid_symbols"] == ["SOL"]
        assert "SOL" in g["goal_text"]

    def test_alts_rotate(self):
        a = select_goal(3)   # pretrade_alts at index 3 (day 3)
        b = select_goal(8)   # next alt block (day 8)
        assert a["kind"] == "pre_trade" and b["kind"] == "pre_trade"
        assert a["paid_symbols"] != b["paid_symbols"]


class TestFindings:

    def test_compact_verdict_maps_factors_to_subtools(self):
        v = {"verdict": "ok", "factors": {
            "liquidity": {"level": "ok", "reason": "deep"},
            "carry": {"level": "caution", "reason": "elevated"},
            "security": {"level": "skipped", "reason": "no token_address"},
        }}
        c = compact_verdict(v)
        assert c["verdict"] == "ok"
        tools = {s["tool"]: s for s in c["subtools"]}
        assert tools["orderbook_depth"]["reading"] == "deep"
        assert tools["funding_rates"]["level"] == "caution"
        assert tools["token_security"]["level"] == "skipped"

    def test_build_findings_pre_trade(self):
        f = build_findings("pre_trade", [], {"BTC": {"verdict": "ok", "factors": {}}})
        assert "BTC" in f["verdicts"]

    def test_build_findings_regime(self):
        calls = [
            {"tool": "fear_greed_index", "params": {}, "data": {"value": 61, "value_classification": "Greed"}},
            {"tool": "gas_tracker", "params": {}, "data": {"standard_gwei": 2.4}},
            {"tool": "defi_tvl", "params": {}, "data": {"tvl": 118_000_000_000.0}},
        ]
        r = build_findings("regime", calls, {})["regime"]
        assert r["fear_greed"] == 61 and r["fear_greed_label"] == "Greed"
        assert r["gas_gwei"] == 2.4 and r["defi_tvl_usd"] == 118_000_000_000.0

    def test_build_findings_crowding_groups_by_symbol(self):
        calls = [
            {"tool": "open_interest", "params": {"symbol": "BTC"}, "data": {"total_oi_usd": 1e10, "oi_change_24h_pct": 3.2, "long_short_ratio": 1.1}},
            {"tool": "orderbook_depth", "params": {"symbol": "BTC"}, "data": {"spread_pct": 0.01}},
        ]
        c = build_findings("crowding", calls, {})["crowding"]
        assert c["BTC"]["oi_usd"] == 1e10 and c["BTC"]["spread_pct"] == 0.01

    def test_funding_bias(self):
        assert _funding_bias({"rates": [{"sentiment": "bearish"}, {"sentiment": "bearish"}, {"sentiment": "bullish"}]}) == "crowded-long"
        assert _funding_bias({"rates": []}) is None


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
