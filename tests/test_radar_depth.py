"""
AGE-138 phase 0b — on-chain payer depth in ranking, fleet flag, provider_map
aggregation, holding dict + flush, prober ingest of providers.

Control numbers are the 2026-08-27 calibration (x402scan source):
  Otto     290 payers · payer_quality 0.828 · retention 0.69 · 11,956 legs → 30/payer
  ApiToll   74 payers · 0.873 · 0.69 · 903 legs → 12/payer
  Cluster  721 payers · 0.999 · 0.98 · 125,208 legs → 174/payer (fleet-shaped)
  fleet     24 payers · 1.000 · 1.00 · 21,302 legs → 888/payer
The spec'd retention multiplier was dropped after that calibration: two rows
with the same payer_quality and different retention MUST score the same.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gateway import radar
from gateway.services import provider_map as pm
from gateway.services import supabase as sb

NOW = datetime.now(timezone.utc)
OTTO, APITOLL, CLUSTER, FLEET = ("0x" + c * 40 for c in "abcd")


def _depth_row(pay_to, payers, pq, retention, legs, age_days=0, **extra):
    return {"pay_to": pay_to, "network": "eip155:8453", "window_days": 30,
            "payers": payers, "legs": legs, "payer_quality": pq, "retention": retention,
            "effective_payers": round(payers * pq, 1), "prober_share": 0.1,
            "total_payers_30d": payers, "total_legs_30d": legs,
            "updated_at": (NOW - timedelta(days=age_days)).isoformat(), "source": "x402scan",
            **extra}


DEPTH = {
    OTTO:    _depth_row(OTTO, 290, 0.828, 0.69, 11956),
    APITOLL: _depth_row(APITOLL, 74, 0.873, 0.69, 903),
    CLUSTER: _depth_row(CLUSTER, 721, 0.999, 0.98, 125208),
    FLEET:   _depth_row(FLEET, 24, 1.0, 1.0, 21302),
}


def _cand(pay_to, payers, calls, url=None, name=None, price="0.001"):
    return {"name": name or url or pay_to[:6], "url": url or f"https://{pay_to[2:8]}.example/x",
            "price_usd": Decimal(price), "network": "eip155:8453", "network_caip2": "eip155:8453",
            "pay_to": pay_to, "tags": [], "has_schema": True,
            "calls30d": calls, "payers30d": payers, "last_called": NOW.isoformat()}


def _quality(cands, depth=None):
    scored, _ = radar.decide(cands, Decimal("0.25"), depth=depth)
    return {s["pay_to"]: s for s in scored}


# ── fresh_depth ──────────────────────────────────────────────────────────────

def test_fresh_depth_missing_or_stale_is_none():
    assert radar.fresh_depth(None, OTTO) is None
    assert radar.fresh_depth({}, OTTO) is None
    assert radar.fresh_depth(DEPTH, "0x" + "9" * 40) is None
    stale = {OTTO: _depth_row(OTTO, 290, 0.828, 0.69, 11956, age_days=8)}
    assert radar.fresh_depth(stale, OTTO) is None
    edge = {OTTO: _depth_row(OTTO, 290, 0.828, 0.69, 11956, age_days=7)}
    assert radar.fresh_depth(edge, OTTO) is not None
    assert radar.fresh_depth(DEPTH, OTTO.upper()) is not None      # case-insensitive


def test_fresh_depth_accepts_postgrest_fraction_lengths():
    row = _depth_row(OTTO, 290, 0.828, 0.69, 11956)
    row["updated_at"] = NOW.strftime("%Y-%m-%dT%H:%M:%S.12345+00:00")   # 5 digits (py3.10 trap)
    assert radar.fresh_depth({OTTO: row}, OTTO) is not None


# ── ranking effect ───────────────────────────────────────────────────────────

def test_payer_quality_scales_the_payer_term_only():
    c = [_cand(OTTO, payers=100, calls=500)]
    base = _quality(c)[OTTO]["quality"]
    with_depth = _quality(c, DEPTH)[OTTO]
    eff = round(100 * 0.828)
    assert with_depth["effective_payers"] == eff
    assert with_depth["quality"] == radar._usage_q(eff, 500, 0)
    assert with_depth["quality"] < base


def test_no_depth_row_and_stale_row_leave_score_unchanged():
    c = [_cand(APITOLL, payers=20, calls=100)]
    base = _quality(c)[APITOLL]["quality"]
    stale = {APITOLL: _depth_row(APITOLL, 74, 0.5, 0.1, 903, age_days=30)}
    assert _quality(c, stale)[APITOLL]["quality"] == base
    assert _quality(c, stale)[APITOLL]["depth"] is None
    assert _quality(c, {OTTO: DEPTH[OTTO]})[APITOLL]["quality"] == base


def test_retention_does_not_affect_rank():
    """The spec'd (0.5 + 0.5·retention) multiplier is gone: same payer_quality,
    different retention → identical quality."""
    a = {OTTO: _depth_row(OTTO, 290, 0.8, 0.0, 5000)}
    b = {OTTO: _depth_row(OTTO, 290, 0.8, 1.0, 5000)}
    c = [_cand(OTTO, payers=50, calls=300)]
    assert _quality(c, a)[OTTO]["quality"] == _quality(c, b)[OTTO]["quality"]


def test_fleet_shaped_flag_is_informational():
    c = [_cand(CLUSTER, payers=50, calls=300), _cand(FLEET, payers=24, calls=480),
         _cand(OTTO, payers=50, calls=300)]
    out = _quality(c, DEPTH)
    assert radar.FLAG_FLEET_SHAPED in out[CLUSTER]["flags"]
    assert radar.FLAG_FLEET_SHAPED in out[FLEET]["flags"]
    assert radar.FLAG_FLEET_SHAPED not in out[OTTO]["flags"]
    # No rank change from the flag itself: Cluster (pq 0.999) ≈ unweighted.
    assert out[CLUSTER]["quality"] == radar._usage_q(50, 300, 0)
    assert out[CLUSTER]["depth"]["fleet_shaped"] is True
    assert out[CLUSTER]["depth"]["legs_per_payer"] == pytest.approx(173.7, abs=0.1)


def test_why_and_public_projection_carry_depth():
    c = [_cand(OTTO, payers=100, calls=500)]
    s = _quality(c, DEPTH)[OTTO]
    assert "290 on-chain payers" in s["why"]
    assert "weighted to 83" in s["why"]
    assert "69% came back" in s["why"]
    assert "41 settled calls per payer" in s["why"]
    assert "fleet-shaped" not in s["why"]
    pub = radar._public(s)
    assert pub["depth"]["retention"] == 0.69
    assert pub["depth"]["payer_quality"] == 0.828
    assert pub["effective_payers"] == 83
    # Cluster's why says so.
    s2 = _quality([_cand(CLUSTER, payers=50, calls=300)], DEPTH)[CLUSTER]
    assert "fleet-shaped: could be one operator's agents" in s2["why"]


def test_rank_from_payload_and_verified_route_accept_depth():
    payload = {"resources": [{
        "resource": {"url": f"https://{OTTO[2:8]}.example/x", "serviceName": "otto"},
        "accepts": [{"network": "eip155:8453", "amount": "1000", "payTo": OTTO,
                     "outputSchema": {"properties": {"ok": {}}}}],
        "quality": {"l30DaysTotalCalls": 500, "l30DaysUniquePayers": 100,
                    "lastCalledAt": NOW.isoformat()},
    }]}
    r = radar.rank_from_payload(payload, "x", Decimal("0.25"), depth=DEPTH)
    assert r["results"][0]["effective_payers"] == 83
    v = radar.verified_route_from_payloads([payload], "x", Decimal("0.25"), depth=DEPTH)
    assert v["recommendation"]["depth"]["payers"] == 290


# ── provider_map aggregation ────────────────────────────────────────────────

def test_providers_from_candidates_groups_by_payto_and_network():
    cands = [_cand(OTTO, 10, 50, url="https://x402.otto.example/a", name="Otto A"),
             _cand(OTTO, 30, 80, url="https://x402.otto.example/b", name="Otto A"),
             _cand(OTTO, 5, 5, url="https://alt.otto.example/c", name="Otto C"),
             _cand(APITOLL, 20, 100, url="https://api.toll.example/p", name="Toll")]
    cands[0]["flags"] = ["factory"]
    rows = {r["pay_to"]: r for r in radar.providers_from_candidates(cands, need="weather", source="sweep")}
    o = rows[OTTO]
    assert o["network"] == "eip155:8453"
    assert o["host"] == "x402.otto.example"          # most listings
    assert o["display_name"] == "Otto A"
    assert o["listings"] == 3 and len(o["resource_urls"]) == 3
    assert o["categories"] == {"weather": 3}
    assert o["sources"] == ["sweep"]
    assert o["evidence"]["payers30d"] == 30 and o["evidence"]["calls30d"] == 135
    assert o["evidence"]["flags"] == ["factory"]
    assert rows[APITOLL]["listings"] == 1


def test_providers_from_results_merges_needs():
    ranked = {
        "weather": [radar._public({**_cand(OTTO, 10, 50, url="https://o.example/w"),
                                   "flags": [], "quality": 1, "delivery": None})],
        "token price": [radar._public({**_cand(OTTO, 40, 90, url="https://o.example/p"),
                                       "flags": [], "quality": 1, "delivery": None})],
    }
    rows = radar.providers_from_results(ranked)
    assert len(rows) == 1
    r = rows[0]
    assert r["categories"] == {"weather": 1, "token price": 1}
    assert r["listings"] == 2 and r["sources"] == ["prober"]
    assert r["evidence"]["payers30d"] == 40


def test_merge_provider_rows_preserves_first_seen_claims_and_adds_counts():
    now = NOW.isoformat()
    new = {"pay_to": OTTO, "network": "eip155:8453", "host": "o.example",
           "display_name": "Otto", "resource_urls": ["https://o.example/a"],
           "categories": {"weather": 1}, "sources": ["sweep"], "listings": 1,
           "evidence": {"payers30d": 10, "flags": ["factory"]}}
    first = sb.merge_provider_rows(None, new, now)
    assert first["first_seen"] == now and first["last_seen"] == now
    stored = {**first, "first_seen": "2026-08-01T00:00:00+00:00", "claimed_by": "otto",
              "claim_proof": {"sig": "x"}, "display_name": "Otto (claimed)"}
    later = sb.merge_provider_rows(stored, {**new, "resource_urls": ["https://o.example/b"],
                                            "categories": {"weather": 2, "news": 1},
                                            "sources": ["prober"],
                                            "evidence": {"payers30d": 12, "flags": ["fleet_shaped"]}},
                                   "2026-09-01T00:00:00+00:00")
    assert later["first_seen"] == "2026-08-01T00:00:00+00:00"
    assert later["last_seen"] == "2026-09-01T00:00:00+00:00"
    assert later["resource_urls"] == ["https://o.example/a", "https://o.example/b"]
    assert later["categories"] == {"weather": 3, "news": 1}
    assert later["sources"] == ["sweep", "prober"]
    assert later["claimed_by"] == "otto" and later["claim_proof"] == {"sig": "x"}
    assert later["display_name"] == "Otto (claimed)"      # stored name wins
    assert later["evidence"]["payers30d"] == 12
    assert later["evidence"]["flags"] == ["factory", "fleet_shaped"]
    assert later["listings"] == 2


# ── holding dict + flush ────────────────────────────────────────────────────

async def test_provider_map_remember_and_flush(monkeypatch):
    pm._pending.clear()
    written: list[list[dict]] = []

    async def _upsert(rows):
        written.append(rows)
        return len(rows)
    monkeypatch.setattr(sb, "upsert_provider_map", _upsert)
    monkeypatch.setattr(sb, "sb_enabled", lambda: True)

    row = lambda url, need: radar.providers_from_candidates(   # noqa: E731
        [_cand(OTTO, 10, 50, url=url)], need=need)[0]
    assert pm.remember([row("https://o.example/a", "weather")]) == 1
    assert pm.remember([row("https://o.example/b", "weather"),
                        row("https://o.example/a", "news")]) == 0   # merged, not added
    assert pm.pending_count() == 1
    assert await pm.flush() == 1
    merged = written[0][0]
    assert sorted(merged["resource_urls"]) == ["https://o.example/a", "https://o.example/b"]
    assert merged["categories"] == {"weather": 2, "news": 1}
    assert pm.pending_count() == 0


async def test_provider_map_flush_requeues_on_failure(monkeypatch):
    pm._pending.clear()

    async def _fail(rows):
        return 0
    monkeypatch.setattr(sb, "upsert_provider_map", _fail)
    monkeypatch.setattr(sb, "sb_enabled", lambda: True)
    pm.remember(radar.providers_from_candidates([_cand(OTTO, 1, 1)], need="x"))
    assert await pm.flush() == 0
    assert pm.pending_count() == 1
    pm._pending.clear()


def test_provider_map_remember_is_bounded(monkeypatch):
    pm._pending.clear()
    monkeypatch.setattr(pm, "_MAX_PENDING", 2)
    rows = [radar.providers_from_candidates([_cand("0x" + f"{i:040x}", 1, 1)], need="x")[0]
            for i in range(5)]
    assert pm.remember(rows) == 2
    assert pm.pending_count() == 2
    pm._pending.clear()
