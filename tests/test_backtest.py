"""
tests/test_backtest.py — pure-function tests for the flagship v2 backtest engine.

No network, no wallet: deterministic synthetic series exercise entry/exit logic,
the equity-curve metrics, and the parameter sweep.
"""

from agents.analyst import backtest


def _bars(closes, start="2026-01-01"):
    # Build ascending daily bars from a close list.
    import datetime as dt
    d0 = dt.date.fromisoformat(start)
    return [{"date": (d0 + dt.timedelta(days=i)).isoformat(), "close": c}
            for i, c in enumerate(closes)]


# ── entry / exit ────────────────────────────────────────────────────────────────

def test_greed_exit_realizes_a_winning_trade():
    bars = _bars([100, 100, 110, 121, 121, 121])
    fg = {bars[0]["date"]: 50, bars[1]["date"]: 20, bars[2]["date"]: 50,
          bars[3]["date"]: 80, bars[4]["date"]: 50, bars[5]["date"]: 50}
    out = backtest.simulate(bars, fg, fear_entry=25, greed_exit=75, hold_days_max=14)
    m, trades = out["metrics"], out["trades"]
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "greed"
    assert trades[0]["return_pct"] == 21.0       # 121/100 - 1
    assert m["total_return_pct"] == 21.0
    assert m["win_rate_pct"] == 100.0
    assert m["n_trades"] == 1
    assert m["max_drawdown_pct"] == 0.0


def test_max_hold_exit_when_greed_never_hits():
    bars = _bars([100, 100, 105, 110, 115])
    # enter on day 2 (fg 10), greed never reached → exit on hold_days_max
    fg = {bars[0]["date"]: 50, bars[1]["date"]: 10, bars[2]["date"]: 40,
          bars[3]["date"]: 45, bars[4]["date"]: 50}
    out = backtest.simulate(bars, fg, fear_entry=25, greed_exit=90, hold_days_max=2)
    trades = out["trades"]
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "max_hold"
    assert trades[0]["held_days"] == 2


def test_no_entry_when_never_fearful():
    bars = _bars([100, 101, 102, 103])
    fg = {b["date"]: 60 for b in bars}
    out = backtest.simulate(bars, fg, fear_entry=25)
    assert out["trades"] == []
    assert out["metrics"]["total_return_pct"] == 0.0
    assert out["metrics"]["sharpe"] == 0.0
    assert out["metrics"]["exposure_pct"] == 0.0


def test_open_position_marked_to_market_at_end():
    bars = _bars([100, 90, 95, 120])      # enter day2 (fg low), still open at end
    fg = {bars[0]["date"]: 50, bars[1]["date"]: 15, bars[2]["date"]: 40, bars[3]["date"]: 50}
    out = backtest.simulate(bars, fg, fear_entry=25, greed_exit=99, hold_days_max=99)
    assert len(out["trades"]) == 1
    assert out["trades"][0]["exit_reason"] == "open_at_end"
    assert out["trades"][0]["return_pct"] == round((120 / 90 - 1) * 100, 2)


# ── sweep ───────────────────────────────────────────────────────────────────────

def test_sweep_covers_grid_and_picks_a_best():
    bars = _bars([100, 90, 110, 95, 130, 90, 140])
    fg = {bars[0]["date"]: 50, bars[1]["date"]: 20, bars[2]["date"]: 78,
          bars[3]["date"]: 22, bars[4]["date"]: 80, bars[5]["date"]: 18, bars[6]["date"]: 60}
    res = backtest.sweep(bars, fg)
    assert res["n_combos"] == 27            # 3 x 3 x 3
    assert res["best"] is not None
    # best must actually be the top-ranked by (sharpe, total_return)
    top = max(res["results"], key=lambda m: (m["sharpe"], m["total_return_pct"]))
    assert res["best"]["sharpe"] == top["sharpe"]


def test_summarize_shape():
    bars = _bars([100, 90, 110, 95, 130])
    fg = {bars[0]["date"]: 50, bars[1]["date"]: 20, bars[2]["date"]: 78,
          bars[3]["date"]: 22, bars[4]["date"]: 80}
    s = backtest.summarize(backtest.sweep(bars, fg))
    assert set(s["best_params"]) == {"fear_entry", "greed_exit", "hold_days_max"}
    assert "sharpe" in s and "combos_tested" in s
    assert s["combos_tested"] == 27


# ── buy-and-hold benchmark ──────────────────────────────────────────────────────

def test_buy_and_hold_full_window_return():
    bars = _bars([100, 110, 121])     # +21% holding through
    m = backtest.buy_and_hold(bars)
    assert m["total_return_pct"] == 21.0
    assert m["exposure_pct"] == 100.0


def test_summarize_includes_benchmark_and_edge():
    # A falling market: strategy is partly in cash, so it should lose LESS than
    # holding — negative absolute, but a positive edge over buy-and-hold.
    bars = _bars([100, 95, 90, 80, 70, 60])
    fg = {bars[0]["date"]: 50, bars[1]["date"]: 50, bars[2]["date"]: 20,
          bars[3]["date"]: 80, bars[4]["date"]: 50, bars[5]["date"]: 50}
    s = backtest.summarize(backtest.sweep(bars, fg))
    assert s["benchmark"]["kind"] == "buy_and_hold"
    assert s["benchmark"]["total_return_pct"] == round((60 / 100 - 1) * 100, 2)  # -40%
    assert "edge" in s
    # strategy sat out most of the crash → beats hold on return and drawdown
    assert s["edge"]["beats_hold_return"] is True
    assert s["edge"]["excess_return_pct"] > 0
    assert s["edge"]["lower_drawdown"] is True
