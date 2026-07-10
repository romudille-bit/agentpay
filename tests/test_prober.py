"""Unit tests for agents/prober/probe.py — the pure prober logic.

Everything here runs offline against synthetic payloads (same convention as
test_radar.py). The I/O runner (agents/prober/run.py) is exercised only where
its helpers are pure enough to call directly.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agents.prober import probe


def cand(url="https://api.example.com/tools/x", pay_to="0xabc", price="0.01", **kw):
    c = {"name": kw.pop("name", url.rsplit("/", 1)[-1]), "url": url,
         "pay_to": pay_to, "price_usd": Decimal(price), "network": "eip155:8453",
         "accepts": {}}
    c.update(kw)
    return c


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def probe_row(url="https://api.example.com/tools/x", *, days_ago=0, probe_type="paid",
              settle_ok=True, http_ok=True, response_nonempty=True, schema_ok=None,
              latency_ms=500):
    return {
        "resource_url": url,
        "probed_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "probe_type": probe_type,
        "settle_ok": settle_ok, "http_ok": http_ok,
        "response_nonempty": response_nonempty, "schema_ok": schema_ok,
        "latency_ms": latency_ms,
    }


# ── select_candidates ──────────────────────────────────────────────────────────

class TestSelectCandidates:
    def test_top_k_per_need_and_cap(self):
        ranked = {
            "web search": [cand(url=f"https://a.com/{i}", pay_to=f"0x{i}") for i in range(5)],
            "token price": [cand(url=f"https://b.com/{i}", pay_to=f"0xb{i}") for i in range(5)],
        }
        sel = probe.select_candidates(ranked, max_paid=4, top_k=3)
        # 3 per need enter t0; paid capped at 4
        assert len(sel["t0"]) == 6
        assert len(sel["t1"]) == 4

    def test_dedup_by_host_and_payto(self):
        # Same host + same wallet listed under two needs = ONE probe
        ranked = {
            "news": [cand(url="https://dup.com/a", pay_to="0x1")],
            "market data": [cand(url="https://dup.com/b", pay_to="0x1"),
                            cand(url="https://other.com/a", pay_to="0x1")],
        }
        sel = probe.select_candidates(ranked)
        assert len(sel["t0"]) == 2  # dup.com/0x1 collapsed; other.com/0x1 kept

    def test_recent_recommendations_take_priority(self):
        ranked = {"n": [cand(url=f"https://x.com/{i}", pay_to=f"0x{i}") for i in range(3)]}
        recent = [cand(url="https://rec.com/tool", pay_to="0xrec")]
        sel = probe.select_candidates(ranked, recent, max_paid=1)
        assert sel["t1"] == [recent[0]]  # freshness guarantee outranks sweep order

    def test_deterministic_across_dict_order(self):
        r1 = {"b": [cand(url="https://b.com/x", pay_to="0xb")],
              "a": [cand(url="https://a.com/x", pay_to="0xa")]}
        r2 = dict(reversed(list(r1.items())))
        assert [c["url"] for c in probe.select_candidates(r1)["t1"]] == \
               [c["url"] for c in probe.select_candidates(r2)["t1"]]

    def test_skips_empty_urls(self):
        sel = probe.select_candidates({"n": [{"url": "", "pay_to": "0x1"}]})
        assert sel["t0"] == [] and sel["t1"] == []


# ── t0_checks ──────────────────────────────────────────────────────────────────

WELLFORMED_402 = {
    "accepts": [{"scheme": "exact", "network": "eip155:8453",
                 "amount": "10000", "payTo": "0xabc",
                 "asset": "0x8335…2913"}],
}


class TestT0Checks:
    def test_wellformed_402(self):
        r = probe.t0_checks(402, WELLFORMED_402, Decimal("0.01"))
        assert r == {"alive": True, "x402_wellformed": True,
                     "price_matches": True, "mpp_option": False,
                     "usdg_option": False}

    def test_dead_endpoint(self):
        r = probe.t0_checks(None, None)
        assert r["alive"] is False and r["x402_wellformed"] is False

    def test_200_is_alive_but_not_x402(self):
        r = probe.t0_checks(200, {"hello": "world"})
        assert r["alive"] is True and r["x402_wellformed"] is False

    def test_402_without_parseable_accepts_is_malformed(self):
        assert probe.t0_checks(402, {"accepts": []})["x402_wellformed"] is False
        assert probe.t0_checks(402, "not json")["x402_wellformed"] is False
        # amount 0 / missing network = not sane
        assert probe.t0_checks(
            402, {"accepts": [{"amount": "0", "network": "eip155:8453"}]}
        )["x402_wellformed"] is False

    def test_price_mismatch_is_flagged(self):
        r = probe.t0_checks(402, WELLFORMED_402, Decimal("0.05"))
        assert r["price_matches"] is False

    def test_price_unknown_is_none_not_false(self):
        assert probe.t0_checks(402, WELLFORMED_402, None)["price_matches"] is None

    def test_decimal_amount_string_parses(self):
        body = {"accepts": [{"amount": "0.01", "network": "eip155:8453"}]}
        r = probe.t0_checks(402, body, Decimal("0.01"))
        assert r["x402_wellformed"] is True and r["price_matches"] is True

    def test_body_as_string_is_parsed(self):
        import json
        r = probe.t0_checks(402, json.dumps(WELLFORMED_402), Decimal("0.01"))
        assert r["x402_wellformed"] is True

    def test_mpp_option_detected_in_payment_options(self):
        # [MR-3] OneSource pattern: MPP/Tempo alongside Base USDC
        body = {
            "accepts": [WELLFORMED_402["accepts"][0]],
            "payment_options": [{"network": "tempo", "scheme": "mpp",
                                 "amount": "10000", "asset": "pathUSD"}],
        }
        assert probe.t0_checks(402, body)["mpp_option"] is True

    def test_no_mpp_on_plain_base(self):
        assert probe.t0_checks(402, WELLFORMED_402)["mpp_option"] is False


# ── t1_evaluate ────────────────────────────────────────────────────────────────

class TestT1Evaluate:
    def test_nonempty_json(self):
        r = probe.t1_evaluate({"price": 3000.5})
        assert r == {"response_nonempty": True, "schema_ok": None}

    def test_empty_dict_is_empty(self):
        assert probe.t1_evaluate({})["response_nonempty"] is False

    def test_short_string_is_empty(self):
        assert probe.t1_evaluate(" ")["response_nonempty"] is False
        assert probe.t1_evaluate("real text")["response_nonempty"] is True

    def test_schema_soft_match_50pct(self):
        schema = {"properties": {"a": {}, "b": {}, "c": {}, "d": {}}}
        assert probe.t1_evaluate({"a": 1, "b": 2}, schema)["schema_ok"] is True   # 2/4
        assert probe.t1_evaluate({"a": 1}, schema)["schema_ok"] is False          # 1/4

    def test_no_schema_means_none_not_penalty(self):
        assert probe.t1_evaluate({"x": 1}, None)["schema_ok"] is None
        assert probe.t1_evaluate({"x": 1}, {})["schema_ok"] is None

    def test_bazaar_info_output_shape(self):
        schema = {"output": {"price_usd": "number", "symbol": "string"}}
        assert probe.t1_evaluate({"price_usd": 1, "symbol": "ETH"},
                                 schema)["schema_ok"] is True

    def test_non_dict_response_fails_advertised_schema(self):
        assert probe.t1_evaluate("text", {"properties": {"a": {}}})["schema_ok"] is False


# ── delivery_factor / score ────────────────────────────────────────────────────

class TestDeliveryFactor:
    def test_unprobed_neutral(self):
        assert probe.delivery_factor(None, probed=False) == 1.0

    def test_good(self):
        assert probe.delivery_factor(1.0, True) == 1.15
        assert probe.delivery_factor(0.9, True) == 1.15

    def test_sliding_band(self):
        # 1.00 − 0.5×(0.9 − rate)
        assert probe.delivery_factor(0.7, True) == 0.9
        assert probe.delivery_factor(0.5, True) == 0.8

    def test_heavy_downrank(self):
        assert probe.delivery_factor(0.49, True) == 0.25
        assert probe.delivery_factor(0.0, True) == 0.25


class TestScore:
    def test_perfect_service(self):
        rows = probe.score([probe_row(days_ago=i) for i in range(4)], now=NOW)
        assert len(rows) == 1
        r = rows[0]
        assert r["paid_probes"] == 4
        assert r["delivery_rate"] == 1.0
        assert r["delivery_factor"] == 1.15
        assert r["flags"] == []
        assert r["latency_p50_ms"] == 500

    def test_paid_but_no_data_flags(self):
        rows = probe.score([
            probe_row(),
            probe_row(settle_ok=True, http_ok=False, response_nonempty=False),
        ], now=NOW)
        assert probe.FLAG_NO_DELIVERY in rows[0]["flags"]
        assert rows[0]["delivery_rate"] == 0.5

    def test_schema_false_counts_against_none_does_not(self):
        ok = probe.score([probe_row(schema_ok=None)], now=NOW)[0]
        bad = probe.score([probe_row(schema_ok=False)], now=NOW)[0]
        assert ok["delivery_rate"] == 1.0
        assert bad["delivery_rate"] == 0.0

    def test_window_excludes_old_probes(self):
        rows = probe.score([
            probe_row(days_ago=0),
            probe_row(days_ago=45, settle_ok=True, http_ok=False),  # outside 30d
        ], now=NOW)
        assert rows[0]["paid_probes"] == 1
        assert rows[0]["flags"] == []

    def test_free_probes_do_not_feed_delivery_rate(self):
        rows = probe.score([
            probe_row(probe_type="free", settle_ok=None, http_ok=None,
                      response_nonempty=None),
        ], now=NOW)
        assert rows[0]["paid_probes"] == 0
        assert rows[0]["delivery_rate"] is None
        assert rows[0]["delivery_factor"] == 1.0  # unprobed = neutral

    def test_last_ok_and_fail_timestamps(self):
        rows = probe.score([
            probe_row(days_ago=2),
            probe_row(days_ago=1, settle_ok=False, http_ok=False,
                      response_nonempty=False),
        ], now=NOW)
        r = rows[0]
        assert r["last_ok_at"] == (NOW - timedelta(days=2)).isoformat()
        assert r["last_fail_at"] == (NOW - timedelta(days=1)).isoformat()

    def test_groups_by_url_sorted(self):
        rows = probe.score([
            probe_row(url="https://b.com/x"),
            probe_row(url="https://a.com/x"),
        ], now=NOW)
        assert [r["resource_url"] for r in rows] == ["https://a.com/x", "https://b.com/x"]


# ── paid_but_no_data ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("settle,http,expected", [
    (True, False, True),   # took money, gave nothing — the worst flag
    (True, True, False),
    (False, False, False),
    (False, True, False),
])
def test_paid_but_no_data(settle, http, expected):
    assert probe.paid_but_no_data(settle, http) is expected


class TestScoreMppAndPrice:
    """AGE-8: [MR-3] mpp label + last-known price aggregate into score rows."""

    def test_mpp_aggregates_from_free_probes(self):
        rows = probe.score([
            probe_row(probe_type="free", settle_ok=None, http_ok=None,
                      response_nonempty=None) | {"mpp_option": True},
            probe_row(days_ago=1),
        ], now=NOW)
        assert rows[0]["mpp_option"] is True

    def test_no_mpp_when_never_advertised(self):
        rows = probe.score([probe_row()], now=NOW)
        assert rows[0]["mpp_option"] is False

    def test_price_is_last_known(self):
        rows = probe.score([
            probe_row(days_ago=5) | {"price_usdc": "0.01"},
            probe_row(days_ago=1) | {"price_usdc": "0.02"},   # newer wins
            probe_row(days_ago=0),                            # no price → ignored
        ], now=NOW)
        assert rows[0]["price_usdc"] == "0.02"

    def test_price_none_when_unknown(self):
        rows = probe.score([probe_row()], now=NOW)
        assert rows[0]["price_usdc"] is None


class TestUsdgLabel:
    """AGE-18: USDG/Robinhood Chain label — exact mirror of the MPP label."""

    def test_usdg_detected_by_network(self):
        body = {"accepts": [WELLFORMED_402["accepts"][0],
                            {"network": "eip155:46630", "amount": "10000",
                             "asset": "0xusdg"}]}
        assert probe.t0_checks(402, body)["usdg_option"] is True

    def test_usdg_detected_by_asset_name(self):
        body = {"accepts": [WELLFORMED_402["accepts"][0]],
                "payment_options": [{"network": "robinhood", "amount": "10000",
                                     "asset": "USDG"}]}
        assert probe.t0_checks(402, body)["usdg_option"] is True

    def test_no_usdg_on_plain_base(self):
        assert probe.t0_checks(402, WELLFORMED_402)["usdg_option"] is False

    def test_usdg_aggregates_into_score(self):
        rows = probe.score([
            probe_row(probe_type="free", settle_ok=None, http_ok=None,
                      response_nonempty=None) | {"usdg_option": True},
            probe_row(days_ago=1),
        ], now=NOW)
        assert rows[0]["usdg_option"] is True
        assert rows[0]["mpp_option"] is False


class TestHeaderOnly402:
    """First live sweep (2026-07-10): 10/15 sellers put payment requirements
    ONLY in the PAYMENT-REQUIRED header (base64 x402 v2), empty body."""

    def _b64(self, payload):
        import base64, json
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def test_header_only_402_is_wellformed(self):
        hdr = {"PAYMENT-REQUIRED": self._b64(WELLFORMED_402)}
        r = probe.t0_checks(402, "{}", Decimal("0.01"), headers=hdr)
        assert r["x402_wellformed"] is True
        assert r["price_matches"] is True

    def test_header_case_insensitive_and_x_prefixed(self):
        hdr = {"x-payment-required": self._b64(WELLFORMED_402)}
        assert probe.t0_checks(402, None, headers=hdr)["x402_wellformed"] is True

    def test_raw_json_header_tolerated(self):
        import json
        hdr = {"PAYMENT-REQUIRED": json.dumps(WELLFORMED_402)}
        assert probe.t0_checks(402, None, headers=hdr)["x402_wellformed"] is True

    def test_garbage_header_ignored(self):
        assert probe.t0_checks(402, None,
                               headers={"PAYMENT-REQUIRED": "%%%"})["x402_wellformed"] is False

    def test_mpp_detected_from_header(self):
        payload = {"accepts": [{"network": "tempo", "scheme": "mpp",
                                "amount": "10000", "asset": "pathUSD"}]}
        hdr = {"PAYMENT-REQUIRED": self._b64(payload)}
        assert probe.t0_checks(402, None, headers=hdr)["mpp_option"] is True


def test_score_ignores_skipped_rows():
    """Skipped (unscoreable) paid rows never enter delivery_rate — a
    buyer-side rejection must not downrank the seller (2026-07-10 incident:
    13 sellers scored 0.25 from our own generic-params 400s)."""
    rows = probe.score([
        probe_row() | {"skipped": True, "settle_ok": None, "http_ok": None,
                       "response_nonempty": None},
    ], now=NOW)
    assert rows[0]["paid_probes"] == 0
    assert rows[0]["delivery_rate"] is None
    assert rows[0]["delivery_factor"] == 1.0
