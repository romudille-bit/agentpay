"""
tests/test_flagship_strategy.py — pure-function tests for flagship v2 strategy.

No wallet, no network: exercises the honest-routing table, CMC URL builders,
defensive CMC parsers, and the backtestable strategy-spec assembler.
"""

from agents.analyst import strategy
from agents.analyst.run import goal_strategy_spec, _GOAL_BUILDERS, _ROTATION, select_goal


# ── honest routing ─────────────────────────────────────────────────────────────

def test_prices_and_regime_route_free():
    assert strategy.route_decision("spot_price")["decision"] == "free"
    assert strategy.route_decision("market_regime")["decision"] == "free"


def test_dex_data_routes_paid_to_cmc():
    d = strategy.route_decision("dex_pair_liquidity")
    assert d["decision"] == "paid"
    assert d["source"].startswith("cmc:")


def test_unknown_need_defaults_free():
    assert strategy.route_decision("something_new")["decision"] == "free"


def test_paid_needs_filters_correctly():
    needs = ["spot_price", "market_regime", "dex_pair_liquidity", "dex_token_discovery"]
    assert strategy.paid_needs(needs) == ["dex_pair_liquidity", "dex_token_discovery"]


# ── CMC URL builders ────────────────────────────────────────────────────────────

def test_cmc_url_builds_base_and_query():
    u = strategy.cmc_url("dex_search", {"q": "BNB"})
    assert u.startswith("https://pro-api.coinmarketcap.com/x402/v1/dex/search")
    assert "q=BNB" in u


def test_cmc_url_accepts_raw_path():
    assert strategy.cmc_url("/x402/custom") == "https://pro-api.coinmarketcap.com/x402/custom"


# ── defensive CMC parsers ───────────────────────────────────────────────────────

def test_parse_dex_search_tolerates_shapes():
    data = {"data": [{"name": "BNB", "symbol": "BNB",
                      "contract_address": "0xbb", "network_slug": "bsc"}]}
    out = strategy.parse_dex_search(data)
    assert out and out[0]["symbol"] == "BNB" and out[0]["address"] == "0xbb"
    assert strategy.parse_dex_search(None) == []
    assert strategy.parse_dex_search({}) == []


def test_parse_dex_search_live_cmc_format():
    # Live CMC shape (2026-06): data.tks[] with abbreviated keys incl. liquidity,
    # so one dex_search yields token + price + liquidity (no dex_pairs needed).
    data = {"data": {"total": 1, "tks": [
        {"plt": "BSC", "n": "Wrapped BNB", "s": "WBNB", "addr": "0xbb4c",
         "pu": "602.38", "liq": 54590188.7, "v24h": 1.1e8}]}}
    out = strategy.parse_dex_search(data)
    assert out and out[0]["symbol"] == "WBNB"
    assert out[0]["address"] == "0xbb4c"
    assert out[0]["network"] == "BSC"
    assert out[0]["price_usd"] == 602.38
    assert out[0]["liquidity_usd"] == 54590188.7


def test_parse_dex_pair_extracts_liquidity():
    data = {"data": [{"contract_address": "0xpair",
                      "quote": [{"price": 1.23, "liquidity": 5_000_000, "volume_24h": 99}]}]}
    out = strategy.parse_dex_pair(data)
    assert out["price_usd"] == 1.23
    assert out["liquidity_usd"] == 5_000_000
    assert out["pair_address"] == "0xpair"
    assert strategy.parse_dex_pair(None) == {}


# ── regime gate + spec assembly ─────────────────────────────────────────────────

def test_regime_gate_extreme_fear_accumulates():
    g = strategy.regime_gate(12, "crowded-short")
    assert g["entry_bias"] == "accumulate"


def test_regime_gate_extreme_greed_trims():
    assert strategy.regime_gate(82, "balanced")["entry_bias"] == "trim"


def test_regime_gate_missing_is_neutral():
    assert strategy.regime_gate(None, None)["entry_bias"] == "neutral"


def test_build_strategy_spec_caps_size_by_liquidity():
    token = {"symbol": "BNB", "name": "BNB", "address": "0xbb", "network": "bsc"}
    regime = strategy.regime_gate(20, "crowded-short")
    liquidity = {"liquidity_usd": 1_000_000}   # 1% = $10k < $25k cap
    routing = strategy.routing_table(["spot_price", "dex_pair_liquidity"])
    receipt = {"spent": "0.02", "budget": "0.25", "calls": 5}
    spec = strategy.build_strategy_spec(
        token=token, regime=regime, liquidity=liquidity,
        routing=routing, receipt=receipt, run_at="2026-06-15 12:00 UTC")
    assert spec["kind"] == "strategy_spec"
    assert spec["execution"]["max_position_usd"] == 10_000.0
    assert spec["backtest"]["executes_live"] is False
    assert spec["cost"]["spent_usdc"] == "0.02"
    # routing provenance is carried so the cost decision is legible
    assert any(r["decision"] == "paid" for r in spec["data_provenance"])


def test_build_strategy_spec_tolerates_missing_liquidity():
    spec = strategy.build_strategy_spec(
        token={"symbol": "BNB"}, regime=strategy.regime_gate(None, None),
        liquidity={}, routing=[], receipt={}, run_at="now")
    assert spec["execution"]["max_position_usd"] is None


def test_build_strategy_spec_bsc_venue_framing():
    spec = strategy.build_strategy_spec(
        token={"symbol": "BNB"}, regime=strategy.regime_gate(20, None),
        liquidity={}, routing=[], receipt={}, run_at="now")
    assert spec["venue"] == {"chain": "BSC", "dex": "PancakeSwap"}
    assert "BSC/PancakeSwap" in spec["name"]
    assert spec["execution"]["venue"] == "PancakeSwap on BSC"


def test_build_strategy_spec_embeds_backtest_results():
    bt = {"best_params": {"fear_entry": 20, "greed_exit": 75, "hold_days_max": 14},
          "total_return_pct": 18.5, "sharpe": 1.4, "max_drawdown_pct": 9.2,
          "win_rate_pct": 62.5, "n_trades": 8, "combos_tested": 27}
    spec = strategy.build_strategy_spec(
        token={"symbol": "BNB"}, regime=strategy.regime_gate(20, None),
        liquidity={}, routing=[], receipt={}, run_at="now", backtest_results=bt)
    assert spec["backtest"]["results"]["sharpe"] == 1.4
    assert spec["backtest"]["executes_live"] is False     # still a spec, not live trading


def test_build_strategy_spec_without_results_has_no_results_key():
    spec = strategy.build_strategy_spec(
        token={"symbol": "BNB"}, regime=strategy.regime_gate(20, None),
        liquidity={}, routing=[], receipt={}, run_at="now")
    assert "results" not in spec["backtest"]


# ── goal wiring ─────────────────────────────────────────────────────────────────

def test_strategy_goal_registered_force_only():
    assert "strategy_spec" in _GOAL_BUILDERS
    assert "strategy_spec" not in _ROTATION          # never auto-rotates / auto-spends


def test_strategy_goal_shape():
    spec = goal_strategy_spec(0, None)
    assert spec["kind"] == "strategy"
    assert spec["paid_symbols"] == []                # paid path is verified_route + CMC
    assert spec["target_token"] == "BNB"
    assert all(t in {"fear_greed_index", "funding_rates", "market_snapshot", "crypto_news"}
               for t, _ in spec["free_tools"])


def test_select_goal_force_strategy():
    spec = select_goal(0, force="strategy_spec")
    assert spec["name"] == "strategy_spec" and spec["kind"] == "strategy"
