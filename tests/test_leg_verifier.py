"""
AGE-142 — chain verification of off-gateway receipt legs.

Covers the PURE parts: the matcher (hash → amount+payTo → amount, consuming
transfers), needs_check, and the ledger view's three-way label + totals.
The RPC I/O is exercised by tools/verify_ledger_legs.py against Base.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gateway.routes import ledger
from gateway.services import leg_verifier as lv


WALLET = "0x" + "a" * 40
SELLER = "0x" + "c" * 40
OTHER = "0x" + "d" * 40


def _t(value: int, to: str = SELLER, h: str = "0xt1") -> dict:
    return {"to": to, "value": str(value), "hash": h, "timeStamp": "1"}


# ── match_legs ───────────────────────────────────────────────────────────────

def test_match_by_hash_wins_and_consumes():
    breakdown = [
        {"tool": "https://s.example/a", "cost": "$0.01", "tx_hash": "0xAAA"},
        {"tool": "https://s.example/b", "cost": "$0.01"},
    ]
    transfers = [_t(10_000, h="0xaaa"), _t(10_000, h="0xbbb")]
    out = lv.match_legs(breakdown, transfers)
    assert [(r["leg_index"], r["method"], r["tx_hash"]) for r in out] == [
        (1, "hash", "0xaaa"),
        (2, "amount", "0xbbb"),       # the hash-matched transfer was consumed
    ]


def test_match_by_amount_and_payto_before_amount_only():
    breakdown = [
        {"tool": "https://s.example/a", "cost": "$0.005"},   # seller known
        {"tool": "https://u.example/b", "cost": "$0.005"},   # seller unknown
    ]
    transfers = [_t(5_000, to=OTHER, h="0x1"), _t(5_000, to=SELLER, h="0x2")]
    out = lv.match_legs(breakdown, transfers, {"https://s.example/a": SELLER})
    assert {r["leg_index"]: (r["method"], r["tx_hash"]) for r in out} == {
        1: ("amount+payto", "0x2"),
        2: ("amount", "0x1"),
    }


def test_no_force_match_on_amount_mismatch():
    breakdown = [{"tool": "https://s.example/a", "cost": "$0.02"}]
    assert lv.match_legs(breakdown, [_t(10_000)]) == []


def test_free_legs_are_skipped_and_indices_are_receipt_positions():
    breakdown = [
        {"tool": "token_price", "cost": "$0.000"},
        {"tool": "https://s.example/a", "cost": "$0.01"},
    ]
    out = lv.match_legs(breakdown, [_t(10_000, h="0x9")])
    assert out == [{"leg_index": 2, "tx_hash": "0x9", "to": SELLER,
                    "amount_usdc": "0.01", "method": "amount"}]


def test_one_transfer_backs_at_most_one_leg():
    breakdown = [{"tool": "x", "cost": "$0.01"}, {"tool": "y", "cost": "$0.01"}]
    out = lv.match_legs(breakdown, [_t(10_000, h="0x1")])
    assert len(out) == 1


# ── needs_check ──────────────────────────────────────────────────────────────

def _meta(run_at="2026-08-20T06:00:00+00:00", legs=1, wallet=WALLET):
    return {"run_at": run_at, "wallet": wallet,
            "objective": {"kind": "probe_sweep"},
            "receipt": {"breakdown": [{"tool": f"https://s.example/{i}", "cost": "$0.01"}
                                      for i in range(legs)]}}


def test_needs_check_never_checked():
    assert lv.needs_check(_meta(), {}) is True


def test_needs_check_all_matched_is_done():
    m = _meta(legs=2)
    k = lv.run_key(m["run_at"])
    existing = {(k, -1): {"verified_at": "2026-08-20T07:00:00+00:00"},
                (k, 1): {}, (k, 2): {}}
    assert lv.needs_check(m, existing) is False


def test_needs_check_rechecks_stale_partial():
    m = _meta(legs=2)
    k = lv.run_key(m["run_at"])
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    assert lv.needs_check(m, {(k, -1): {"verified_at": old}, (k, 1): {}}) is True
    assert lv.needs_check(m, {(k, -1): {"verified_at": fresh}, (k, 1): {}}) is False


def test_needs_check_no_paid_legs():
    assert lv.needs_check({"run_at": "2026-08-20T06:00:00+00:00",
                           "receipt": {"breakdown": [{"tool": "t", "cost": "$0"}]}}, {}) is False


def test_run_key_normalises_fractional_digits():
    assert lv.run_key("2026-08-20T06:00:00.12345+00:00") == lv.run_key("2026-08-20T06:00:00.123450+00:00")


def test_run_wallet_prefers_meta_then_fallback():
    assert lv.run_wallet({"wallet": WALLET}, ["0x" + "f" * 40]) == WALLET
    assert lv.run_wallet({"wallet": "GABC"}, ["GABC", "0x" + "f" * 40]) == "0x" + "f" * 40
    assert lv.run_wallet({}, []) is None


# ── ledger view: three-way label + totals ───────────────────────────────────

def _breakdown():
    return [
        {"tool": "pre_trade_check", "cost": "$0.01", "tx_hash": "0xgw", "network": "eip155:8453"},
        {"tool": "https://s.example/a", "cost": "$0.01", "network": "eip155:8453"},
        {"tool": "https://s.example/b", "cost": "$0.01", "network": "eip155:8453"},
    ]


def test_view_labels_gateway_chain_and_attested():
    vlegs = Counter({("0xgw", Decimal("0.01")): 1})
    chain = {2: {"tx_hash": "0xchain", "amount_usdc": "0.01", "method": "amount+payto"}}
    view = ledger._run_view_from_breakdown(_breakdown(), Decimal("0.25"), vlegs, chain)
    labels = [(p["verification"], p["verification_method"]) for p in view["paid_calls"]]
    assert labels == [("onchain", "gateway"),
                      ("onchain_chain", "chain:amount+payto"),
                      ("agent_attested", None)]
    assert view["paid_calls"][1]["tx_hash"] == "0xchain"
    assert view["paid_calls"][1]["explorer_url"].endswith("/tx/0xchain")
    assert view["paid_calls"][2]["explorer_url"] is None
    assert view["verified_spent_usdc"] == "0.02"
    assert view["chain_verified_spent_usdc"] == "0.01"
    assert view["attested_spent_usdc"] == "0.01"
    assert view["has_chain_verified_spend"] is True


def test_view_ignores_chain_row_with_mismatched_amount():
    chain = {2: {"tx_hash": "0xchain", "amount_usdc": "0.05", "method": "amount"}}
    view = ledger._run_view_from_breakdown(_breakdown(), Decimal("0.25"), Counter(), chain)
    assert view["paid_calls"][1]["verification"] == "agent_attested"
    assert view["paid_calls"][1]["tx_hash"] is None


def test_view_checked_run_marks_unmatched_leg_no_settlement_found():
    """AGE-142: once the verifier has checked a run (marker row -1 present),
    an unmatched paid leg is 'no_settlement_found' — booked fail-closed, never
    settled — not 'agent_attested' (which now means 'not checked yet')."""
    chain = {-1: {"method": "checked"},
             2: {"tx_hash": "0xchain", "amount_usdc": "0.01", "method": "amount"}}
    view = ledger._run_view_from_breakdown(_breakdown(), Decimal("0.25"), Counter(), chain)
    labels = [p["verification"] for p in view["paid_calls"]]
    assert labels == ["no_settlement_found", "onchain_chain", "no_settlement_found"]
    assert view["unsettled_spent_usdc"] == "0.02"
    assert view["unsettled_paid_count"] == 2
    assert view["attested_spent_usdc"] == "0.00"
    assert view["has_unsettled_spend"] is True
    assert view["paid_calls"][0]["explorer_url"] is None


def test_synthesize_uses_chain_index_for_its_run():
    meta = {"run_at": "2026-08-20T06:00:00.5+00:00",
            "objective": {"kind": "probe_sweep", "cap_usdc": "0.50"},
            "receipt": {"breakdown": _breakdown()[1:]}}
    k = lv.run_key(meta["run_at"])
    chain_index = {(k, 1): {"tx_hash": "0x1", "amount_usdc": "0.01", "method": "hash"},
                   (k, -1): {"method": "checked"},
                   (lv.run_key("2026-08-21T06:00:00+00:00"), 2): {"tx_hash": "0xother", "amount_usdc": "0.01", "method": "hash"}}
    runs: list = []
    ledger.synthesize_offgateway_runs(runs, [meta], chain_index=chain_index)
    calls = runs[0]["paid_calls"]
    assert calls[0]["verification"] == "onchain_chain"
    # This run WAS checked (marker present) → unmatched leg = no settlement
    # found; the other run's row is not applied to it.
    assert calls[1]["verification"] == "no_settlement_found"


def test_totals_split_gateway_chain_attested_unsettled():
    data = {"runs": [
        {"spent_usdc": "0.04", "attested_spent_usdc": "0.01",
         "chain_verified_spent_usdc": "0.01", "attested_paid_count": 1,
         "unsettled_spent_usdc": "0.01", "unsettled_paid_count": 1,
         "paid_count": 4, "free_count": 0},
        {"spent_usdc": "0.02", "paid_count": 2, "free_count": 4},   # pure payment_logs run
    ]}
    ledger._recompute_totals(data)
    t = data["totals"]
    assert t["spent_usdc"] == "0.06"
    assert t["unsettled_spent_usdc"] == "0.01"
    assert t["settled_spent_usdc"] == "0.05"
    assert t["verified_spent_usdc"] == "0.04"          # spent − attested − unsettled
    assert t["chain_verified_spent_usdc"] == "0.01"
    assert t["gateway_verified_spent_usdc"] == "0.03"
    assert t["attested_spent_usdc"] == "0.01"
    assert t["attested_paid_calls"] == 1
    assert t["unsettled_paid_calls"] == 1
    assert t["verified_share"] == "0.667"
    assert t["verified_share_of_settled"] == "0.800"
