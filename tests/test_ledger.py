"""
tests/test_ledger.py — flagship receipt ledger.

Two layers:
  * group_runs() pure-function tests (no I/O): run clustering, free/paid split,
    spend-vs-cap, explorer links, ordering.
  * route tests via TestClient: HTML served, JSON shape, LEDGER_ENABLED 404 gate.

The synthetic rows mirror real flagship payment_logs shape: free intel calls
log at $0 on Stellar under the agent's Stellar identity; paid pre_trade_check
verdicts settle $0.01 on Base (eip155:8453). Abandoned challenge legs (state !=
'payment_done') must be ignored.
"""

from decimal import Decimal

from fastapi.testclient import TestClient

from gateway.routes import ledger
from gateway.config import settings


# ── helpers ──────────────────────────────────────────────────────────────────

def _free(ts, tool="fear_greed_index"):
    return {"created_at": ts, "tool_name": tool, "network": "stellar-mainnet",
            "amount_usdc": "0.000", "state": "payment_done", "tx_hash": None,
            "agent_address": "GAACF3K43CEWDO2BMOGT3K3GSETBINQFXZ3EQFJUWFLYNTCRHRAA3KVD"}


def _paid(ts, tx, amount="0.01"):
    return {"created_at": ts, "tool_name": "pre_trade_check", "network": "eip155:8453",
            "amount_usdc": amount, "state": "payment_done", "tx_hash": tx,
            "agent_address": "0xe1601C10B8d4DbF71E0c592B779520380174bc3A"}


# One run = 3 free intel + 2 paid verdicts in a ~30s burst.
RUN_A = [
    _free("2026-06-12T18:16:30+00:00", "fear_greed_index"),
    _free("2026-06-12T18:16:35+00:00", "funding_rates"),
    _free("2026-06-12T18:16:40+00:00", "market_snapshot"),
    _paid("2026-06-12T18:16:49+00:00", "0xf4056b2bb4766e71"),
    _paid("2026-06-12T18:16:53+00:00", "0xef7b0a3a80d1a55a"),
]
RUN_B = [
    _free("2026-06-13T13:04:35+00:00", "fear_greed_index"),
    _free("2026-06-13T13:04:40+00:00", "funding_rates"),
    _paid("2026-06-13T13:04:52+00:00", "0xd9a5e5e7efba68d3"),
    _paid("2026-06-13T13:05:03+00:00", "0x9d53dae136644c3c"),
]


# ── group_runs: clustering ───────────────────────────────────────────────────

def test_two_runs_separated_by_gap():
    out = group = ledger.group_runs(RUN_A + RUN_B)
    assert out["totals"]["runs"] == 2
    # newest run first
    assert out["runs"][0]["started"].startswith("2026-06-13")
    assert out["runs"][1]["started"].startswith("2026-06-12")


def test_single_burst_is_one_run():
    out = ledger.group_runs(RUN_A)
    assert out["totals"]["runs"] == 1
    r = out["runs"][0]
    assert r["free_count"] == 3
    assert r["paid_count"] == 2


def test_unordered_input_still_clusters():
    shuffled = [RUN_B[2], RUN_A[0], RUN_B[0], RUN_A[3], RUN_A[1], RUN_B[3], RUN_A[2], RUN_A[4], RUN_B[1]]
    out = ledger.group_runs(shuffled)
    assert out["totals"]["runs"] == 2
    assert out["totals"]["paid_calls"] == 4
    assert out["totals"]["free_calls"] == 5


# ── group_runs: money + cap ──────────────────────────────────────────────────

def test_spend_and_cap():
    out = ledger.group_runs(RUN_A, run_cap="0.25")
    r = out["runs"][0]
    assert r["spent_usdc"] == "0.02"
    assert r["cap_usdc"] == "0.25"
    assert r["under_cap"] is True
    assert out["totals"]["spent_usdc"] == "0.02"


def test_over_cap_flagged():
    rows = [_paid(f"2026-06-12T18:16:{30+i:02d}+00:00", f"0x{i:02d}") for i in range(30)]
    out = ledger.group_runs(rows, run_cap="0.25")
    r = out["runs"][0]
    assert Decimal(r["spent_usdc"]) == Decimal("0.30")
    assert r["under_cap"] is False


# ── group_runs: filtering + links ────────────────────────────────────────────

def test_only_completed_rows_count():
    abandoned = {"created_at": "2026-06-13T13:04:49+00:00", "tool_name": "pre_trade_check",
                 "network": "stellar-mainnet", "amount_usdc": "0.01", "state": "abandoned",
                 "tx_hash": None, "agent_address": None}
    out = ledger.group_runs(RUN_B + [abandoned])
    assert out["totals"]["paid_calls"] == 2  # abandoned excluded


def test_base_explorer_link():
    out = ledger.group_runs(RUN_B)
    paid = out["runs"][0]["paid_calls"][0]
    assert paid["network"] == "base"
    assert paid["explorer_url"] == "https://basescan.org/tx/0xd9a5e5e7efba68d3"


def test_empty_input():
    out = ledger.group_runs([])
    assert out["totals"]["runs"] == 0
    assert out["totals"]["spent_usdc"] == "0.00"
    assert out["runs"] == []


def test_parse_ts_variable_fractional_digits():
    # 5-digit microseconds + offset (real Postgres shape) must parse, not drop.
    assert ledger._parse_ts("2026-06-12T22:30:35.61428+00:00") is not None
    assert ledger._parse_ts("2026-06-12T18:16:53.209657+00:00") is not None
    assert ledger._parse_ts("2026-06-13T13:04:35Z") is not None
    assert ledger._parse_ts("garbage") is None


def test_four_hour_gap_splits_even_with_odd_timestamps():
    rows = [
        _free("2026-06-12T18:16:33.449487+00:00"),
        _paid("2026-06-12T18:16:53.209657+00:00", "0xa"),
        _free("2026-06-12T22:30:35.61428+00:00"),   # 5-digit micros
        _paid("2026-06-12T22:30:41.243794+00:00", "0xb"),
    ]
    out = ledger.group_runs(rows)
    assert out["totals"]["runs"] == 2


def test_running_budget_drawdown():
    out = ledger.group_runs(RUN_B, run_cap="0.25")
    paid = out["runs"][0]["paid_calls"]
    assert paid[0]["remaining_after_usdc"] == "0.24"
    assert paid[1]["remaining_after_usdc"] == "0.23"
    assert out["runs"][0]["remaining_usdc"] == "0.23"


# ── execution timeline ───────────────────────────────────────────────────────

def test_timeline_orders_all_calls_with_budget_drawdown():
    out = ledger.group_runs(RUN_B, run_cap="0.25")
    tl = out["runs"][0]["timeline"]
    # 2 free + 2 paid = 4 steps, in execution order, numbered from 1
    assert [s["step"] for s in tl] == [1, 2, 3, 4]
    assert [s["kind"] for s in tl] == ["free", "free", "paid", "paid"]
    # budget only draws down on paid steps
    assert [s["remaining_usdc"] for s in tl] == ["0.25", "0.25", "0.24", "0.23"]
    # purposes are human-readable, not bare tool names
    assert tl[0]["purpose"] == "read market sentiment"        # fear_greed_index
    assert tl[2]["purpose"] == "buy a trade-safety verdict"   # pre_trade_check
    # paid steps carry their on-chain link
    assert tl[2]["explorer_url"].startswith("https://basescan.org/tx/")
    assert "explorer_url" not in tl[0]


def test_timeline_unknown_tool_gets_fallback_purpose():
    rows = [{"created_at": "2026-06-13T13:04:35+00:00", "tool_name": "mystery_tool",
             "network": "stellar-mainnet", "amount_usdc": "0.000",
             "state": "payment_done", "tx_hash": None, "agent_address": "G"}]
    tl = ledger.group_runs(rows)["runs"][0]["timeline"]
    assert tl[0]["purpose"] == "call mystery_tool"


def test_attach_reasoning_includes_objective():
    out = ledger.group_runs(RUN_B)
    m = _meta("2026-06-13T13:04:40+00:00", {"BTC": {"verdict": "ok", "factors": {}}})
    m["objective"] = {"symbols": ["BTC", "ETH"], "trade_size_usd": 25000,
                      "side": "long", "cap_usdc": "0.25"}
    ledger.attach_reasoning(out["runs"], [m])
    assert out["runs"][0]["reasoning"]["objective"]["trade_size_usd"] == 25000


# ── attach_reasoning (merge) ─────────────────────────────────────────────────

def _meta(run_at, verdicts):
    return {"run_at": run_at, "wallet": "0xe16", "max_spend": "0.25",
            "plan": {"total_usdc": "0.02", "steps": ["a", "b"], "fits_budget": True},
            "regime": "Fear & Greed 55 (Greed)", "context": "12 headlines (net bullish)",
            "verdicts": verdicts, "skipped": {}, "receipt": {"spent": "$0.020"},
            "free_intel": {"tools": ["crypto_news", "gas_tracker"]}, "note": "n"}


def test_attach_reasoning_matches_by_time_window():
    out = ledger.group_runs(RUN_A + RUN_B)
    metas = [
        _meta("2026-06-13T13:04:40+00:00", {"BTC": {"verdict": "ok", "factors": {}}}),
        _meta("2026-06-12T18:16:40+00:00", {"ETH": {"verdict": "caution", "factors": {}}}),
    ]
    n = ledger.attach_reasoning(out["runs"], metas)
    assert n == 2
    assert out["runs"][0]["reasoning"]["regime"].startswith("Fear & Greed")
    assert "BTC" in out["runs"][0]["reasoning"]["verdicts"]


def test_attach_reasoning_no_match_leaves_runs_bare():
    out = ledger.group_runs(RUN_B)
    n = ledger.attach_reasoning(out["runs"], [_meta("2020-01-01T00:00:00+00:00", {})])
    assert n == 0
    assert "reasoning" not in out["runs"][0]


# ── reconcile_from_receipt (off-gateway CMC spend) ───────────────────────────

def _strat_paid(ts, tx, tool="verified_route"):
    return {"created_at": ts, "tool_name": tool, "network": "eip155:8453",
            "amount_usdc": "0.01", "state": "payment_done", "tx_hash": tx,
            "agent_address": "0xe1601C10B8d4DbF71E0c592B779520380174bc3A"}


def test_reconcile_adds_offgateway_cmc_legs_from_receipt():
    # payment_logs sees only the gateway verified_route leg; the two direct CMC
    # x402 legs settle off-gateway and never land here.
    rows = [
        _free("2026-06-15T13:04:35+00:00", "fear_greed_index"),
        _strat_paid("2026-06-15T13:04:50+00:00", "0xroutehash"),
    ]
    out = ledger.group_runs(rows, run_cap="0.25")
    assert out["runs"][0]["paid_count"] == 1          # before reconcile

    meta = {
        "run_at": "2026-06-15T13:04:40+00:00",
        "objective": {"kind": "strategy"},
        "receipt": {
            "calls": 4, "spent": "$0.030", "budget": "$0.250",
            "breakdown": [
                {"tool": "fear_greed_index", "cost": "$0.000", "tx_hash": "", "network": ""},
                {"tool": "verified_route", "cost": "$0.010", "tx_hash": "0xroutehash", "network": "eip155:8453"},
                {"tool": "https://pro-api.coinmarketcap.com/x402/v1/dex/search?q=BNB",
                 "cost": "$0.010", "tx_hash": "0xcmcsearch", "network": "base"},
                {"tool": "https://pro-api.coinmarketcap.com/x402/v4/dex/pairs/quotes/latest?contract_address=0xbb",
                 "cost": "$0.010", "tx_hash": "0xcmcpairs", "network": "base"},
            ],
        },
    }
    assert ledger.attach_reasoning(out["runs"], [meta]) == 1
    assert ledger.reconcile_from_receipt(out["runs"]) == 1

    run = out["runs"][0]
    assert run["reconciled_from_receipt"] is True
    assert run["paid_count"] == 3                      # verified_route + 2 CMC legs
    assert run["free_count"] == 1
    assert run["spent_usdc"] == "0.03"                 # timeline now matches the receipt
    tools = [s["tool"] for s in run["timeline"]]
    assert "cmc_dex_search" in tools and "cmc_dex_pairs" in tools
    cmc = next(s for s in run["timeline"] if s["tool"] == "cmc_dex_search")
    assert cmc["kind"] == "paid" and "basescan.org/tx/0xcmcsearch" in cmc["explorer_url"]


def test_reconcile_skips_non_strategy_runs():
    out = ledger.group_runs(RUN_B, run_cap="0.25")
    meta = {"run_at": "2026-06-13T13:04:40+00:00", "objective": {"kind": "pre_trade"},
            "receipt": {"breakdown": [{"tool": "x", "cost": "$0.010",
                                       "tx_hash": "0xz", "network": "base"}]}}
    ledger.attach_reasoning(out["runs"], [meta])
    assert ledger.reconcile_from_receipt(out["runs"]) == 0


# ── ingest endpoint ──────────────────────────────────────────────────────────

def test_ingest_404_when_secret_unset(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "")
    from gateway.main import app
    assert TestClient(app).post("/v1/flagship/run", json={}).status_code == 404


def test_ingest_401_on_bad_secret(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    from gateway.main import app
    r = TestClient(app).post("/v1/flagship/run", json={"run_at": "x"},
                             headers={"X-Flagship-Secret": "wrong"})
    assert r.status_code == 401


def test_ingest_stores_with_valid_secret(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    captured = {}

    async def _fake_insert(run):
        captured.update(run)
        return True
    monkeypatch.setattr(ledger, "insert_flagship_run", _fake_insert)
    from gateway.main import app
    r = TestClient(app).post("/v1/flagship/run",
                             json={"run_at_iso": "2026-06-13T13:04:40+00:00", "wallet": "0xe16"},
                             headers={"X-Flagship-Secret": "s3cr3t"})
    assert r.status_code == 200
    assert r.json()["stored"] is True
    assert captured["wallet"] == "0xe16"


# ── route tests ──────────────────────────────────────────────────────────────

def test_ledger_html_served(monkeypatch):
    from gateway.main import app
    c = TestClient(app)
    resp = c.get("/ledger")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Budgeted Data Access" in resp.text
    assert "What came back" in resp.text


def test_ledger_json_shape(monkeypatch):
    async def _fake_rows():
        return RUN_A + RUN_B
    monkeypatch.setattr(ledger, "_fetch_flagship_rows", _fake_rows)
    from gateway.main import app
    c = TestClient(app)
    resp = c.get("/ledger.json")
    assert resp.status_code == 200
    d = resp.json()
    assert d["agent"] == "AgentPay flagship analyst"
    assert d["totals"]["runs"] == 2
    assert d["wallets"]["base"].startswith("0x")
    assert d["wallets"]["stellar"].startswith("G")
    assert resp.headers.get("cache-control") == "no-store"


def test_ledger_disabled_404(monkeypatch):
    monkeypatch.setattr(settings, "LEDGER_ENABLED", False)
    from gateway.main import app
    c = TestClient(app)
    assert c.get("/ledger").status_code == 404
    assert c.get("/ledger.json").status_code == 404
