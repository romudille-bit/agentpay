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


# ── route tests ──────────────────────────────────────────────────────────────

def test_ledger_html_served(monkeypatch):
    from gateway.main import app
    c = TestClient(app)
    resp = c.get("/ledger")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Flagship Ledger" in resp.text


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
