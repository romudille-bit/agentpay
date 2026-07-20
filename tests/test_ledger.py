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
    # AGE-63: only the gateway-settled leg (0xroutehash @ $0.01, in
    # payment_logs) is verifiable; the two direct CMC legs are agent-attested.
    from collections import Counter
    verified_legs = Counter({("0xroutehash", Decimal("0.01")): 1})
    assert ledger.reconcile_from_receipt(out["runs"], verified_legs) == 1

    run = out["runs"][0]
    assert run["reconciled_from_receipt"] is True
    assert run["paid_count"] == 3                      # verified_route + 2 CMC legs
    assert run["free_count"] == 1
    assert run["spent_usdc"] == "0.03"                 # timeline now matches the receipt
    tools = [s["tool"] for s in run["timeline"]]
    assert "cmc_dex_search" in tools and "cmc_dex_pairs" in tools

    # verified_route settled through the gateway → on-chain, explorer link.
    vr = next(s for s in run["timeline"] if s["tool"] == "verified_route")
    assert vr["verification"] == "onchain"
    assert "basescan.org/tx/0xroutehash" in vr["explorer_url"]

    # The CMC leg never touched payment_logs → agent-attested, NO explorer link
    # (a link would falsely read as "AgentPay verified this hash on-chain").
    cmc = next(s for s in run["timeline"] if s["tool"] == "cmc_dex_search")
    assert cmc["kind"] == "paid"
    assert cmc["verification"] == "agent_attested"
    assert cmc["explorer_url"] is None

    # Run-level split: $0.01 verified (verified_route) + $0.02 attested (2 CMC).
    assert run["has_attested_spend"] is True
    assert run["verified_spent_usdc"] == "0.01"
    assert run["attested_spent_usdc"] == "0.02"
    assert run["attested_paid_count"] == 2


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
    ledger._invalidate_ledger_cache()          # AGE-72: isolate from other tests
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
    # AGE-72: short public cache instead of no-store.
    assert resp.headers.get("cache-control") == "public, max-age=60"


def test_ledger_json_is_cached_and_invalidated(monkeypatch):
    """AGE-72: the built payload is cached for a window (no re-query on the
    next hit) and dropped when a new run is ingested."""
    ledger._invalidate_ledger_cache()
    calls = {"n": 0}

    async def _fake_rows():
        calls["n"] += 1
        return RUN_A
    monkeypatch.setattr(ledger, "_fetch_flagship_rows", _fake_rows)
    from gateway.main import app
    c = TestClient(app)

    c.get("/ledger.json")
    c.get("/ledger.json")
    assert calls["n"] == 1                      # second hit served from cache

    ledger._invalidate_ledger_cache()           # e.g. a new run ingested
    c.get("/ledger.json")
    assert calls["n"] == 2                       # rebuilt after invalidation


def test_ledger_disabled_404(monkeypatch):
    monkeypatch.setattr(settings, "LEDGER_ENABLED", False)
    from gateway.main import app
    c = TestClient(app)
    assert c.get("/ledger").status_code == 404
    assert c.get("/ledger.json").status_code == 404


# ── synthesize_offgateway_runs (AGE-10: probe_sweep runs on the ledger) ───────

def _probe_meta(run_at="2026-07-10T18:40:00+00:00", breakdown=None):
    return {
        "run_at": run_at,
        "objective": {"kind": "probe_sweep",
                      "goal_text": "Probe 15 x402 services for delivery quality",
                      "cap_usdc": "0.50"},
        "receipt": {"calls": 1, "spent": "$0.02", "budget": "$0.5",
                    "breakdown": breakdown if breakdown is not None else [
                        {"tool": "https://stablefinance.dev/api/news",
                         "cost": "$0.02", "tx_hash": "0xabc",
                         "network": "eip155:8453"}]},
        "note": "AgentPay prober — sweep",
    }


def test_synthesize_probe_sweep_run_from_receipt():
    runs: list = []          # nothing clustered — probe payments are off-gateway
    added = ledger.synthesize_offgateway_runs(runs, [_probe_meta()], run_cap="0.25")
    assert added == 1
    r = runs[0]
    assert r["synthesized_offgateway"] is True
    assert r["reasoning"]["kind"] == "probe_sweep"
    assert r["cap_usdc"] == "0.50"            # objective cap wins over run_cap
    assert r["spent_usdc"] == "0.02"
    assert r["paid_count"] == 1
    assert r["under_cap"] is True
    assert r["paid_calls"][0]["tx_hash"] == "0xabc"
    assert r["timeline"][0]["kind"] == "paid"


def test_synthesize_skips_metas_inside_existing_windows():
    # A meta whose run_at falls inside a clustered flagship run must NOT be
    # duplicated as a synthetic run (attach_reasoning owns that case).
    out = ledger.group_runs(RUN_A, run_cap="0.25")
    inside = _probe_meta(run_at=out["runs"][0]["started"])
    added = ledger.synthesize_offgateway_runs(out["runs"], [inside])
    assert added == 0


def test_synthesize_ignores_non_probe_metas():
    meta = _probe_meta()
    meta["objective"]["kind"] = "regime"
    runs: list = []
    assert ledger.synthesize_offgateway_runs(runs, [meta]) == 0


def test_synthesize_orders_newest_first():
    out = ledger.group_runs(RUN_A, run_cap="0.25")
    n_before = len(out["runs"])
    added = ledger.synthesize_offgateway_runs(
        out["runs"], [_probe_meta(run_at="2099-01-01T00:00:00+00:00")])
    assert added == 1 and len(out["runs"]) == n_before + 1
    assert out["runs"][0]["synthesized_offgateway"] is True   # newest first


def test_synthesize_handles_empty_breakdown():
    runs: list = []
    added = ledger.synthesize_offgateway_runs(runs, [_probe_meta(breakdown=[])])
    assert added == 1
    assert runs[0]["spent_usdc"] == "0.00"
    assert runs[0]["paid_count"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# AGE-62: /ledger row-cap freeze (order desc + re-sort asc)
# ═════════════════════════════════════════════════════════════════════════════

import httpx
import pytest


@pytest.mark.asyncio
async def test_fetch_flagship_rows_orders_desc_and_resorts_asc(monkeypatch):
    """AGE-62: the Supabase query must order created_at DESC (so the 2000-row
    cap keeps the NEWEST runs), then hand rows back chronologically. Regression
    for the freeze where asc+limit returned the oldest 2000 forever."""
    monkeypatch.setattr(ledger, "sb_enabled", lambda: True)
    monkeypatch.setattr(ledger, "_flagship_addresses", lambda: ["0xe16"])
    monkeypatch.setattr(ledger.settings, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(ledger, "sb_headers", lambda: {})

    seen = {}
    # Supabase returns newest-first (as the desc query asks).
    newest_first = [
        {"created_at": "2026-06-12T18:16:53+00:00", "tool_name": "pre_trade_check",
         "network": "eip155:8453", "amount_usdc": "0.01", "state": "payment_done",
         "tx_hash": "0xb", "agent_address": "0xe16"},
        {"created_at": "2026-06-12T18:16:49+00:00", "tool_name": "pre_trade_check",
         "network": "eip155:8453", "amount_usdc": "0.01", "state": "payment_done",
         "tx_hash": "0xa", "agent_address": "0xe16"},
    ]

    def handler(request):
        seen["order"] = dict(request.url.params).get("order")
        return httpx.Response(200, json=newest_first)

    import respx
    with respx.mock:
        respx.get("https://x.supabase.co/rest/v1/payment_logs").mock(side_effect=handler)
        rows = await ledger._fetch_flagship_rows()

    assert seen["order"] == "created_at.desc"                 # keep newest 2000
    # …but returned chronologically for group_runs.
    assert [r["created_at"] for r in rows] == [
        "2026-06-12T18:16:49+00:00", "2026-06-12T18:16:53+00:00",
    ]


# ═════════════════════════════════════════════════════════════════════════════
# AGE-63: /ledger must not present agent-posted hashes as on-chain-verified
# ═════════════════════════════════════════════════════════════════════════════

def test_run_view_labels_verified_vs_attested():
    from collections import Counter
    breakdown = [
        {"tool": "verified_route", "cost": "$0.010", "tx_hash": "0xGATE",
         "network": "eip155:8453"},
        {"tool": "https://evil.example/x402/fake", "cost": "$0.010",
         "tx_hash": "0xFABRICATED", "network": "base"},
    ]
    # Only the gateway leg is in payment_logs (keyed (hash_lower, amount)).
    view = ledger._run_view_from_breakdown(
        breakdown, Decimal("0.25"),
        verified_legs=Counter({("0xgate", Decimal("0.01")): 1}))

    gate = next(s for s in view["timeline"] if s["tool"] == "verified_route")
    fake = next(s for s in view["timeline"] if s["verification"] == "agent_attested")
    assert gate["verification"] == "onchain" and gate["explorer_url"]
    # A fabricated / off-gateway hash is never dressed up as verified.
    assert fake["verification"] == "agent_attested"
    assert fake["explorer_url"] is None
    assert view["verified_spent_usdc"] == "0.01"
    assert view["attested_spent_usdc"] == "0.01"
    assert view["has_attested_spend"] is True


def test_run_view_reused_real_hash_credited_once():
    """AGE-63 residual: a holder of the ingest secret reuses ONE real gateway
    hash across three legs at a fabricated $0.05 each. Only the single real
    settlement ($0.01) may show on-chain; the rest are agent-attested — a real
    hash can't be replayed to inflate verified spend."""
    from collections import Counter
    breakdown = [
        {"tool": "verified_route", "cost": "$0.050", "tx_hash": "0xREAL", "network": "base"},
        {"tool": "verified_route", "cost": "$0.050", "tx_hash": "0xREAL", "network": "base"},
        {"tool": "verified_route", "cost": "$0.050", "tx_hash": "0xREAL", "network": "base"},
    ]
    # payment_logs has exactly ONE real leg: 0xreal @ $0.01.
    view = ledger._run_view_from_breakdown(
        breakdown, Decimal("0.25"),
        verified_legs=Counter({("0xreal", Decimal("0.01")): 1}))
    verifs = [s["verification"] for s in view["timeline"]]
    # None match: the fabricated cost ($0.05) != the real settled amount ($0.01),
    # so even the reused real hash stays attested.
    assert verifs == ["agent_attested", "agent_attested", "agent_attested"]
    assert view["verified_spent_usdc"] == "0.00"

    # And when the amount DOES match, only the first of the reused legs is
    # credited on-chain; the copies are consumed-out to attested.
    view2 = ledger._run_view_from_breakdown(
        [{"tool": "verified_route", "cost": "$0.010", "tx_hash": "0xREAL", "network": "base"},
         {"tool": "verified_route", "cost": "$0.010", "tx_hash": "0xREAL", "network": "base"}],
        Decimal("0.25"),
        verified_legs=Counter({("0xreal", Decimal("0.01")): 1}))
    assert [s["verification"] for s in view2["timeline"]] == ["onchain", "agent_attested"]
    assert view2["verified_spent_usdc"] == "0.01"


def test_run_view_no_verified_set_means_all_attested():
    """Fail safe: with no verified set, nothing may claim on-chain status."""
    view = ledger._run_view_from_breakdown(
        [{"tool": "verified_route", "cost": "$0.010", "tx_hash": "0xANY",
          "network": "base"}],
        Decimal("0.25"))
    assert view["timeline"][0]["verification"] == "agent_attested"
    assert view["timeline"][0]["explorer_url"] is None


def test_synthesized_probe_leg_is_attested_without_verified_set():
    """A prober sweep pays sellers directly (never in payment_logs) → its legs
    are agent-attested, not falsely on-chain-verified."""
    runs: list = []
    from collections import Counter
    ledger.synthesize_offgateway_runs(runs, [_probe_meta()], run_cap="0.25",
                                      verified_legs=Counter())
    leg = runs[0]["timeline"][0]
    assert leg["kind"] == "paid"
    assert leg["verification"] == "agent_attested"
    assert leg["explorer_url"] is None
    assert runs[0]["has_attested_spend"] is True


def test_recompute_totals_splits_verified_and_attested():
    data = {"runs": [
        {"spent_usdc": "0.03", "paid_count": 3, "free_count": 1,
         "attested_spent_usdc": "0.02"},                    # reconciled run
        {"spent_usdc": "0.02", "paid_count": 2, "free_count": 3},  # pure on-chain
    ]}
    ledger._recompute_totals(data)
    t = data["totals"]
    assert t["spent_usdc"] == "0.05"
    assert t["attested_spent_usdc"] == "0.02"
    assert t["verified_spent_usdc"] == "0.03"


def test_ledger_html_marks_attested_legs(monkeypatch):
    """The public page must render the agent-attested badge + explanatory note
    (not an explorer link) for off-gateway legs."""
    async def _rows():
        return []
    async def _metas():
        return [_probe_meta()]
    monkeypatch.setattr(ledger, "_fetch_flagship_rows", _rows)
    monkeypatch.setattr(ledger, "fetch_flagship_runs", _metas)
    monkeypatch.setattr(ledger.settings, "LEDGER_ENABLED", True)
    from gateway.main import app
    html = TestClient(app).get("/ledger").text
    assert "agent-attested" in html
    assert "tatt" in html            # the badge class ships in the page


# ── AGE-63 ingest idempotency ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_insert_flagship_run_skips_duplicate_run_at(monkeypatch):
    """A retried POST for a run_at that already exists is an idempotent no-op:
    the existence check finds it and NO insert fires (so /ledger can't show the
    run twice or double-count totals)."""
    import gateway.services.supabase as sb
    monkeypatch.setattr(sb, "sb_enabled", lambda: True)
    monkeypatch.setattr(sb.settings, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(sb, "sb_headers", lambda: {})

    posted = []
    import respx
    with respx.mock:
        respx.get("https://x.supabase.co/rest/v1/flagship_runs").mock(
            return_value=httpx.Response(200, json=[{"run_at": "2026-06-15T13:04:40+00:00"}]))
        respx.post("https://x.supabase.co/rest/v1/flagship_runs").mock(
            side_effect=lambda req: (posted.append(1), httpx.Response(201))[1])
        ok = await sb.insert_flagship_run(
            {"run_at_iso": "2026-06-15T13:04:40+00:00", "wallet": "0xe16"})

    assert ok is True            # reported success…
    assert posted == []          # …but nothing inserted (idempotent no-op)


@pytest.mark.asyncio
async def test_insert_flagship_run_inserts_when_new(monkeypatch):
    """A run_at not yet stored: existence check is empty → insert proceeds."""
    import gateway.services.supabase as sb
    monkeypatch.setattr(sb, "sb_enabled", lambda: True)
    monkeypatch.setattr(sb.settings, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(sb, "sb_headers", lambda: {})

    posted = []
    import respx
    with respx.mock:
        respx.get("https://x.supabase.co/rest/v1/flagship_runs").mock(
            return_value=httpx.Response(200, json=[]))   # not stored yet
        respx.post("https://x.supabase.co/rest/v1/flagship_runs").mock(
            side_effect=lambda req: (posted.append(1), httpx.Response(201))[1])
        ok = await sb.insert_flagship_run(
            {"run_at_iso": "2026-06-15T13:04:40+00:00", "wallet": "0xe16"})

    assert ok is True and posted == [1]


@pytest.mark.asyncio
async def test_insert_flagship_run_no_run_at_skips_existence_check(monkeypatch):
    """A payload with no run_at can't be idempotency-keyed — no existence
    check (no accidental broad read), just the insert."""
    import gateway.services.supabase as sb
    monkeypatch.setattr(sb, "sb_enabled", lambda: True)
    monkeypatch.setattr(sb.settings, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(sb, "sb_headers", lambda: {})

    gets = []
    import respx
    with respx.mock:
        respx.get("https://x.supabase.co/rest/v1/flagship_runs").mock(
            side_effect=lambda req: (gets.append(1), httpx.Response(200, json=[]))[1])
        respx.post("https://x.supabase.co/rest/v1/flagship_runs").mock(
            return_value=httpx.Response(201))
        ok = await sb.insert_flagship_run({"wallet": "0xe16"})   # no run_at

    assert ok is True
    assert gets == []


# ═════════════════════════════════════════════════════════════════════════════
# F4 (follow-up review 2026-07-20): cross-view hash reuse must not double-count
# ═════════════════════════════════════════════════════════════════════════════

def test_preconsume_blocks_cross_view_hash_reuse():
    """The verified_legs Counter is seeded from EVERY flagship payment_logs
    leg, but legs rendered in ordinary (non-reconciled) runs were never
    consumed from it. A holder of FLAGSHIP_INGEST_SECRET could post a
    fabricated receipt reusing a real run's public tx_hash + amount and get
    that one settlement rendered "onchain" (explorer link) and counted as
    verified spend twice. preconsume_rendered_legs consumes the ordinary
    runs' legs first, so the reused hash stays agent_attested."""
    from collections import Counter
    rows = [
        _paid("2026-06-15T09:00:00+00:00", "0xREAL"),        # ordinary run
        _strat_paid("2026-06-15T15:00:00+00:00", "0xGATE"),  # strategy run (6h later)
    ]
    out = ledger.group_runs(rows, run_cap="0.25")
    assert len(out["runs"]) == 2

    meta = {
        "run_at": "2026-06-15T15:00:00+00:00",
        "objective": {"kind": "strategy"},
        "receipt": {"breakdown": [
            {"tool": "verified_route", "cost": "$0.010", "tx_hash": "0xGATE",
             "network": "eip155:8453"},
            # The attack: reuse the ordinary run's public hash + real amount.
            {"tool": "verified_route", "cost": "$0.010", "tx_hash": "0xREAL",
             "network": "eip155:8453"},
        ]},
    }
    assert ledger.attach_reasoning(out["runs"], [meta]) == 1

    verified_legs = Counter({("0xreal", Decimal("0.01")): 1,
                             ("0xgate", Decimal("0.01")): 1})
    # Mirrors ledger_json order: preconsume, then reconcile.
    assert ledger.preconsume_rendered_legs(out["runs"], verified_legs) == 1
    assert ledger.reconcile_from_receipt(out["runs"], verified_legs) == 1

    strat = next(r for r in out["runs"] if r.get("reconciled_from_receipt"))
    verifs = {s["tx_hash"]: s["verification"]
              for s in strat["timeline"] if s.get("kind") == "paid"}
    # The strategy run's own gateway leg is still credited...
    assert verifs["0xGATE"] == "onchain"
    # ...but the settlement already displayed by the ordinary run is not
    # credited a second time.
    assert verifs["0xREAL"] == "agent_attested"


def test_preconsume_leaves_reconciled_runs_legs_alone():
    """A strategy run's OWN payment_logs legs must stay in the Counter so
    its receipt legs can still match them — preconsume only consumes for
    runs that keep the payment_logs view."""
    from collections import Counter
    rows = [_strat_paid("2026-06-15T15:00:00+00:00", "0xGATE")]
    out = ledger.group_runs(rows, run_cap="0.25")
    meta = {
        "run_at": "2026-06-15T15:00:00+00:00",
        "objective": {"kind": "strategy"},
        "receipt": {"breakdown": [
            {"tool": "verified_route", "cost": "$0.010", "tx_hash": "0xGATE",
             "network": "eip155:8453"},
        ]},
    }
    assert ledger.attach_reasoning(out["runs"], [meta]) == 1
    verified_legs = Counter({("0xgate", Decimal("0.01")): 1})
    assert ledger.preconsume_rendered_legs(out["runs"], verified_legs) == 0
    assert ledger.reconcile_from_receipt(out["runs"], verified_legs) == 1
    leg = out["runs"][0]["timeline"][0]
    assert leg["verification"] == "onchain"
