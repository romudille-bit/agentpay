"""
AGE-138 — the gateway's own provider_depth refresh (services/depth_refresh.py).

The pure math must match tools/payer_depth.py exactly (same weight CASE, same
aggregates) — the laptop tool and the gateway loop write the same table.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway.services import depth_refresh as dr
from gateway.services import supabase as sb

NOW = datetime.now(timezone.utc)


def _leg(sender, amount=10000, ts="2026-08-20T06:00:00.000Z", decimals=6):
    return {"sender": sender, "amount": amount, "decimals": decimals,
            "block_timestamp": ts}


def test_payer_weight_matches_the_sql_case():
    assert dr.payer_weight(2, 1.0) == 1.0                    # returning → 1.0 regardless
    assert dr.payer_weight(1, 1.0) == pytest.approx(0.2)     # prober: one leg everywhere
    assert dr.payer_weight(1, 0.0) == pytest.approx(1.0)     # heavy buyer trying us once
    assert dr.payer_weight(1, 0.5) == pytest.approx(0.6)
    assert dr.payer_weight(1, 7.0) == pytest.approx(0.2)     # clamp


def test_pairs_from_legs_aggregates_per_sender():
    legs = [_leg("0xA", 10000, "2026-08-20T06:00:00.000Z"),
            _leg("0xa", 20000, "2026-08-22T06:00:00.000Z"),   # same sender, case-folded
            _leg("0xB", 5000, "2026-08-21T06:00:00.000Z"),
            {"sender": "", "amount": 1}]                      # dropped
    pairs = dr.pairs_from_legs(legs)
    assert set(pairs) == {"0xa", "0xb"}
    assert pairs["0xa"]["legs"] == 2
    assert pairs["0xa"]["usd"] == pytest.approx(0.03)
    assert pairs["0xa"]["first"].startswith("2026-08-20")
    assert pairs["0xa"]["last"].startswith("2026-08-22")


def test_depth_row_matches_tool_semantics():
    # 3 payers: one returning (2 legs), one heavy-buyer-elsewhere (1 leg,
    # fanout ~0), one prober (1 leg, fanout 1.0).
    pairs = {"0xret": {"legs": 2, "usd": 0.02, "first": "a", "last": "b"},
             "0xbuy": {"legs": 1, "usd": 0.01, "first": "a", "last": "b"},
             "0xbot": {"legs": 1, "usd": 0.01, "first": "a", "last": "b"}}
    shape = {"0xbuy": {"tx_count": 200, "unique_sellers": 2},
             "0xbot": {"tx_count": 65, "unique_sellers": 65}}
    total = {"tx_count": 4000, "unique_buyers": 3}
    row = dr.depth_row("0x" + "e" * 40, pairs, shape, total, sampled=True,
                       now_iso=NOW.isoformat())
    assert row["payers"] == 3 and row["legs"] == 4
    assert row["returning_payers"] == 1 and row["retention"] == pytest.approx(0.3333)
    # weights: 1.0 + (0.2+0.8*(1-0.01)) + 0.2 = 1.0 + 0.992 + 0.2
    assert row["effective_payers"] == pytest.approx(2.192)
    assert row["payer_quality"] == pytest.approx(2.192 / 3, abs=1e-4)
    assert row["prober_share"] == pytest.approx(1 / 3, abs=1e-4)
    assert row["top_payer_share"] == pytest.approx(0.5)
    assert row["sampled"] is True
    assert row["total_legs_30d"] == 4000 and row["total_payers_30d"] == 3
    assert row["source"] == "gateway" and row["network"] == "eip155:8453"
    # true-total legs/payer (what radar's fleet flag reads): 4000/3 > 100
    from gateway import radar
    assert radar.depth_legs_per_payer(row) == pytest.approx(4000 / 3)


def test_depth_row_without_shape_falls_back_to_own_legs():
    pairs = {"0xa": {"legs": 1, "usd": 0.01, "first": "a", "last": "b"}}
    row = dr.depth_row("0x" + "e" * 40, pairs, {}, None, False, NOW.isoformat())
    # fanout falls back to 1/legs_here = 1.0 → prober floor
    assert row["effective_payers"] == pytest.approx(0.2)
    assert row["total_legs_30d"] == 1 and row["total_payers_30d"] == 1


def test_newest_row_age_days():
    fresh = {"a": {"updated_at": (NOW - timedelta(days=1)).isoformat()},
             "b": {"updated_at": (NOW - timedelta(days=9)).isoformat()}}
    assert dr._newest_row_age_days(fresh) == 1
    assert dr._newest_row_age_days({}) is None
    assert dr._newest_row_age_days({"a": {"updated_at": None}}) is None


async def test_refresh_pulls_computes_and_upserts(monkeypatch):
    """End-to-end with the x402scan calls stubbed: two payTos in provider_map,
    one with legs, rows land in upsert_provider_depth and the cache clears."""
    P1, P2 = "0x" + "1" * 40, "0x" + "2" * 40

    async def _fetch_map():
        return {(P1, "eip155:8453"): {}, (P2, "eip155:8453"): {},
                ("0x" + "3" * 40, "solana:x"): {}}
    monkeypatch.setattr(sb, "fetch_provider_map", _fetch_map)
    monkeypatch.setattr(sb, "sb_enabled", lambda: True)
    written: list[list[dict]] = []

    async def _upsert(rows):
        written.append(rows)
        return len(rows)
    monkeypatch.setattr(sb, "upsert_provider_depth", _upsert)
    cleared = []
    monkeypatch.setattr(sb, "_depth_cache_clear", lambda: cleared.append(1))
    monkeypatch.setattr(dr, "THROTTLE_S", 0)

    async def _rpc(client, proc, inp):
        if proc == "public.sellers.all.list":
            if "recipients" in inp:      # totals
                return {"items": [{"recipient": P1, "tx_count": 10, "unique_buyers": 2}]}
            return {"items": [{"recipient": P1}]}   # top head (already known)
        if proc == "public.transfers.list":
            pt = inp["recipients"]["include"][0]
            if pt == P1:
                return {"items": [_leg("0xA"), _leg("0xA"), _leg("0xB")],
                        "hasNextPage": False}
            return {"items": [], "hasNextPage": False}
        if proc == "public.buyers.all.list":
            return {"items": [{"sender": "0xa", "tx_count": 2, "unique_sellers": 1},
                              {"sender": "0xb", "tx_count": 1, "unique_sellers": 1}]}
        raise AssertionError(proc)
    monkeypatch.setattr(dr, "_rpc", _rpc)

    n = await dr.refresh()
    assert n == 1 and cleared == [1]
    row = written[0][0]
    assert row["pay_to"] == P1 and row["payers"] == 2
    assert row["returning_payers"] == 1
    assert row["total_legs_30d"] == 10 and row["total_payers_30d"] == 2


async def test_refresh_with_empty_map_and_unreachable_head_writes_nothing(monkeypatch):
    async def _fetch_map():
        return {}
    monkeypatch.setattr(sb, "fetch_provider_map", _fetch_map)
    monkeypatch.setattr(dr, "THROTTLE_S", 0)

    async def _rpc(client, proc, inp):
        raise RuntimeError("down")
    monkeypatch.setattr(dr, "_rpc", _rpc)
    written = []

    async def _upsert(rows):
        written.append(rows)
        return len(rows)
    monkeypatch.setattr(sb, "upsert_provider_depth", _upsert)
    # KNOWN_TRUSTED EVM payTos remain in the set; their legs fetch fails → skipped.
    assert await dr.refresh() == 0
    assert written == []
