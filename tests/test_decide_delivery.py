"""
tests/test_decide_delivery.py — AGE-7: the two decide() fixes.

1. [MR-2] usage reweighting: unique payers dominant
   (usage_q = payers×5 + min(calls, payers×20) + recency; single_payer_volume
   flag when call volume outruns distinct buyers).
2. Delivery factor from the Prober's service_scores (join on resource URL):
   unprobed = neutral 1.0; boost/downrank multiplies quality;
   took_payment_no_delivery = listed, flagged, NEVER recommended; the score
   row surfaces as a human `why` line.

Pure-function tests, no network — same convention as test_verified_route.py.
"""

from decimal import Decimal

from gateway import radar


def _cand(url, payers=10, calls=50, pay_to="0xaaa", price="0.01",
          last_called=None, name=None):
    return {
        "name": name or url.rsplit("/", 1)[-1],
        "url": url,
        "price_usd": Decimal(price),
        "network": "eip155:8453", "network_caip2": "eip155:8453",
        "pay_to": pay_to, "tags": [], "has_schema": True,
        "calls30d": calls, "payers30d": payers, "last_called": last_called,
        "accepts": {},
    }


def _score_row(url, factor=1.15, rate=1.0, probes=8, p50=740, flags=(),
               last_fail=None):
    return {url: {
        "resource_url": url, "window_days": 30, "paid_probes": probes,
        "delivery_rate": rate, "delivery_factor": factor,
        "latency_p50_ms": p50, "flags": list(flags),
        "last_fail_at": last_fail,
    }}


BUDGET = Decimal("1")


# ── [MR-2] usage reweighting ───────────────────────────────────────────────────

class TestUsageReweighting:
    def test_wash_traffic_no_longer_outranks_real_payers(self):
        """The 2026-07-07 sweep case: 342 calls from ONE payer ranked #1 under
        payers×3 + calls. With payers dominant it loses to a modestly-used
        tool with 10 distinct buyers."""
        wash = _cand("https://wash.x/infer", payers=1, calls=342, pay_to="0x111")
        real = _cand("https://real.x/infer", payers=10, calls=30, pay_to="0x222")
        _, rec = radar.decide([wash, real], BUDGET)
        assert rec["url"] == "https://real.x/infer"

    def test_usage_q_formula(self):
        # payers×5 + min(calls, payers×20), no recency
        assert radar._usage_q(10, 30, None) == 80
        assert radar._usage_q(1, 342, None) == 25      # calls capped at 20
        assert radar._usage_q(0, 500, None) == 0       # zero payers = zero volume credit
        assert radar._usage_q(2, 10, 3) == 25          # +5 recency bonus

    def test_single_payer_volume_flag(self):
        wash = _cand("https://wash.x/a", payers=1, calls=342)
        ok = _cand("https://ok.x/a", payers=10, calls=150)
        scored, _ = radar.decide([wash, ok], BUDGET)
        by_url = {s["url"]: s for s in scored}
        assert "single_payer_volume" in by_url["https://wash.x/a"]["flags"]
        assert "single_payer_volume" not in by_url["https://ok.x/a"]["flags"]

    def test_zero_payer_volume_is_flagged_too(self):
        ghost = _cand("https://ghost.x/a", payers=0, calls=100)
        scored, _ = radar.decide([ghost], BUDGET)
        assert "single_payer_volume" in scored[0]["flags"]


# ── delivery factor ────────────────────────────────────────────────────────────

class TestDeliveryFactor:
    def test_unprobed_is_neutral(self):
        c = _cand("https://a.x/t")
        no_scores, _ = radar.decide([c], BUDGET)
        empty_scores, _ = radar.decide([c], BUDGET, scores={})
        assert no_scores[0]["quality"] == empty_scores[0]["quality"]
        assert no_scores[0]["delivery"] is None

    def test_factor_multiplies_quality(self):
        c = _cand("https://a.x/t", payers=10, calls=30)     # usage_q 80
        scored, _ = radar.decide([c], BUDGET,
                                 scores=_score_row("https://a.x/t", factor=1.15))
        assert scored[0]["quality"] == int(80 * 1.15)       # 92
        bad, _ = radar.decide([c], BUDGET,
                              scores=_score_row("https://a.x/t", factor=0.25,
                                                rate=0.4))
        assert bad[0]["quality"] == 20

    def test_downranked_delivery_loses_to_clean_peer(self):
        flaky = _cand("https://flaky.x/t", payers=20, calls=100, pay_to="0x111")
        clean = _cand("https://clean.x/t", payers=15, calls=80, pay_to="0x222")
        scores = {**_score_row("https://flaky.x/t", factor=0.25, rate=0.4),
                  **_score_row("https://clean.x/t", factor=1.15)}
        _, rec = radar.decide([flaky, clean], BUDGET, scores=scores)
        assert rec["url"] == "https://clean.x/t"

    def test_no_delivery_flag_hard_drops_from_recommendation(self):
        thief = _cand("https://thief.x/t", payers=100, calls=900, pay_to="0x111")
        modest = _cand("https://modest.x/t", payers=5, calls=10, pay_to="0x222")
        scores = _score_row("https://thief.x/t", factor=0.25, rate=0.0,
                            flags=[radar.FLAG_NO_DELIVERY],
                            last_fail="2026-07-03T05:00:00+00:00")
        scored, rec = radar.decide([thief, modest], BUDGET, scores=scores)
        # still listed + flagged…
        thief_row = next(s for s in scored if s["url"] == "https://thief.x/t")
        assert radar.FLAG_NO_DELIVERY in thief_row["flags"]
        assert not thief_row["dropped"]
        # …but never recommended
        assert rec["url"] == "https://modest.x/t"

    def test_why_lines(self):
        good = _cand("https://good.x/t")
        thief = _cand("https://thief.x/t", pay_to="0x222")
        scores = {**_score_row("https://good.x/t", probes=8, rate=1.0, p50=740),
                  **_score_row("https://thief.x/t",
                               flags=[radar.FLAG_NO_DELIVERY],
                               last_fail="2026-07-03T05:00:00+00:00")}
        scored, _ = radar.decide([good, thief], BUDGET, scores=scores)
        by_url = {s["url"]: s for s in scored}
        assert by_url["https://good.x/t"]["why"] == \
            "probed 8× in 30d, 100% delivered, median 740ms"
        assert by_url["https://thief.x/t"]["why"] == \
            "⚠ took payment without delivering on 2026-07-03"

    def test_why_and_delivery_survive_public_projection(self):
        c = _cand("https://a.x/t")
        scored, _ = radar.decide([c], BUDGET, scores=_score_row("https://a.x/t"))
        pub = radar._public(scored[0])
        assert pub["why"].startswith("probed 8×")
        assert pub["delivery"]["factor"] == 1.15

    def test_malformed_factor_degrades_to_neutral(self):
        c = _cand("https://a.x/t", payers=10, calls=30)
        scored, _ = radar.decide(
            [c], BUDGET,
            scores={"https://a.x/t": {"delivery_factor": "not-a-number"}})
        assert scored[0]["quality"] == 80


# ── verified_route integration (pure) ─────────────────────────────────────────

def _res(name, url, pay_to, payers, calls):
    return {
        "serviceName": name, "resource": {"url": url},
        "accepts": [{"scheme": "exact", "network": "eip155:8453",
                     "amount": "10000", "payTo": pay_to,
                     "outputSchema": {"type": "object"}}],
        "quality": {"l30DaysUniquePayers": payers, "l30DaysTotalCalls": calls},
        "tags": [],
    }


def test_verified_route_skips_flagged_rec_and_carries_why():
    payload = {"resources": [
        _res("Thief", "https://thief.x/t", "0x111", payers=100, calls=900),
        _res("Honest", "https://honest.x/t", "0x222", payers=40, calls=300),
    ]}
    scores = _score_row("https://thief.x/t", factor=0.25, rate=0.0,
                        flags=[radar.FLAG_NO_DELIVERY],
                        last_fail="2026-07-03T05:00:00+00:00")
    out = radar.verified_route_from_payloads([payload], "data", BUDGET,
                                             scores=scores)
    assert out["recommendation"]["url"] == "https://honest.x/t"
    listed = {s["url"]: s for s in out["survivors"]}
    assert radar.FLAG_NO_DELIVERY in listed["https://thief.x/t"]["flags"]
    assert listed["https://thief.x/t"]["why"].startswith("⚠ took payment")
