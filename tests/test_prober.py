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
        # ALL survivors get a free T0 check; paid set = top-3/need, capped at 4
        assert len(sel["t0"]) == 10
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
        # freshness guarantee outranks sweep order
        assert [c["url"] for c in sel["t1"]] == ["https://rec.com/tool"]
        assert sel["t1"][0]["need"] == "recently recommended"

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

    def test_single_no_delivery_is_unconfirmed(self):
        # AGE-11 policy: 1 paid_but_no_data → unconfirmed flag (rec-drop only,
        # no public accusation — could be a transient outage).
        rows = probe.score([
            probe_row(),
            probe_row(settle_ok=True, http_ok=False, response_nonempty=False),
        ], now=NOW)
        assert probe.FLAG_NO_DELIVERY_UNCONFIRMED in rows[0]["flags"]
        assert probe.FLAG_NO_DELIVERY not in rows[0]["flags"]
        assert rows[0]["delivery_rate"] == 0.5

    def test_two_no_deliveries_confirm_the_public_flag(self):
        rows = probe.score([
            probe_row(days_ago=3, settle_ok=True, http_ok=False,
                      response_nonempty=False),
            probe_row(days_ago=0, settle_ok=True, http_ok=False,
                      response_nonempty=False),
        ], now=NOW)
        assert probe.FLAG_NO_DELIVERY in rows[0]["flags"]
        assert probe.FLAG_NO_DELIVERY_UNCONFIRMED not in rows[0]["flags"]

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
        # AGE-86: last_fail_at marks a SETTLED probe that didn't deliver.
        # An unsettled probe (settle_ok=False) is not the seller's failure
        # and must set neither timestamp.
        rows = probe.score([
            probe_row(days_ago=2),
            probe_row(days_ago=1, settle_ok=True, http_ok=False,
                      response_nonempty=False),      # paid, nothing back
            probe_row(days_ago=0, settle_ok=False, http_ok=False,
                      response_nonempty=False),      # our settle failed
        ], now=NOW)
        r = rows[0]
        assert r["last_ok_at"] == (NOW - timedelta(days=2)).isoformat()
        assert r["last_fail_at"] == (NOW - timedelta(days=1)).isoformat()
        assert r["settle_failures"] == 1

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


class TestNameAndNeed:
    """AGE-20: readable identity — Bazaar serviceName + discovery need flow
    from selection through score rows."""

    def test_select_annotates_need(self):
        ranked = {"web search": [cand(url="https://a.com/x", pay_to="0xa")]}
        recent = [cand(url="https://rec.com/t", pay_to="0xr")]
        sel = probe.select_candidates(ranked, recent)
        by_url = {c["url"]: c for c in sel["t0"]}
        assert by_url["https://a.com/x"]["need"] == "web search"
        assert by_url["https://rec.com/t"]["need"] == "recently recommended"

    def test_score_carries_last_known_name_and_need(self):
        rows = probe.score([
            probe_row(days_ago=2) | {"name": "Old Name", "need": "news"},
            probe_row(days_ago=0) | {"name": "StableFinance", "need": "news"},
        ], now=NOW)
        assert rows[0]["name"] == "StableFinance"
        assert rows[0]["need"] == "news"

    def test_score_name_none_when_unknown(self):
        rows = probe.score([probe_row()], now=NOW)
        assert rows[0]["name"] is None and rows[0]["need"] is None

    def test_score_carries_network(self):
        rows = probe.score([
            probe_row() | {"network": "eip155:8453"},
        ], now=NOW)
        assert rows[0]["network"] == "eip155:8453"


class TestT0BreadthAndParams:
    def test_t0_covers_all_survivors_paid_stays_topk(self):
        ranked = {"n": [cand(url=f"https://s{i}.com/x", pay_to=f"0x{i}")
                        for i in range(10)]}
        sel = probe.select_candidates(ranked, top_k=3, max_paid=15)
        assert len(sel["t0"]) == 10       # every survivor gets a free check
        assert len(sel["t1"]) == 3        # only top-k spend money

    def test_params_for_known_and_unknown_needs(self):
        assert probe.params_for("token price")["symbol"] == "ETH"
        assert "messages" in probe.params_for("llm inference")
        assert probe.params_for("something else") == {}
        assert probe.params_for(None) == {}

    def test_params_for_returns_a_copy(self):
        p = probe.params_for("news")
        p["q"] = "mutated"
        assert probe.params_for("news")["q"] == "crypto"


class TestSelfExclusion:
    """The self-scoring ban is CODE, not policy (2026-07-12): with honest
    Bazaar tags our tools can now legitimately rank for sweep needs, so the
    prober must skip them everywhere — no self-probe, no self-boost, ever."""

    def test_own_host_excluded(self):
        ranked = {"trading": [
            cand(url="https://agentpay.tools/tools/pre_trade_check/call",
                 pay_to="0x9999"),
            cand(url="https://real.x/t", pay_to="0x1"),
        ]}
        sel = probe.select_candidates(ranked)
        urls = [c["url"] for c in sel["t0"]]
        assert urls == ["https://real.x/t"]

    def test_own_wallet_excluded_any_host(self):
        ranked = {"n": [cand(url="https://some-mirror.example/x",
                             pay_to="0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7")]}
        sel = probe.select_candidates(ranked)
        assert sel["t0"] == [] and sel["t1"] == []

    def test_own_service_excluded_from_breadth_tail(self):
        ranked = {"n": [cand(url=f"https://x{i}.com/t", pay_to=f"0x{i}")
                        for i in range(3)] +
                       [cand(url="https://agentpay.tools/v1/session/create",
                             pay_to="0x9999")]}
        sel = probe.select_candidates(ranked, top_k=3)
        assert all("agentpay.tools" not in c["url"] for c in sel["t0"])


# ── AGE-83: delivery-verification coverage ─────────────────────────────────────

class TestCallSpec:
    """The seller publishes how to call it; use that instead of one generic
    guess per need. On the 2026-07-27 sweep 10 of 12 paid settles were burned
    on pre-delivery param rejections."""

    CONCRETE_GET = {"method": "GET", "type": "http",
                    "queryParams": {"url": "https://example.com/file.pdf"}}
    CONCRETE_POST = {"method": "POST", "bodyType": "json", "type": "http",
                     "body": {"url": "https://example.com/f.pdf"}}
    SCHEMA_FORM = {"properties": {"input": {"properties": {
        "method": {"const": "GET"},
        "queryParams": {"properties": {"url": {"type": "string"},
                                       "pages": {"type": "integer"}},
                        "required": ["url"]}}}}}

    def test_advertised_method_is_read(self):
        assert probe.call_spec(cand(input_spec=self.CONCRETE_GET))["method"] == "GET"
        assert probe.call_spec(cand(input_spec=self.CONCRETE_POST))["method"] == "POST"

    def test_our_known_good_value_beats_the_sellers_placeholder(self):
        # The seller's example is example.com — a real probe needs a real PDF.
        spec = probe.call_spec(cand(need="pdf ocr", input_spec=self.CONCRETE_GET))
        assert spec["params"]["url"] == probe.NEED_PARAMS["pdf ocr"]["url"]
        assert spec["source"] == "advertised"

    def test_undeclared_fields_are_dropped(self):
        # NEED_PARAMS shotguns q AND query; a strict validator
        # (additionalProperties: false — seen live) rejects the extra one.
        spec = probe.call_spec(cand(need="web search", input_spec={
            "method": "GET", "queryParams": {"q": "example"}}))
        assert set(spec["params"]) == {"q"}

    def test_schema_form_keys_are_understood(self):
        spec = probe.call_spec(cand(need="pdf ocr", input_spec=self.SCHEMA_FORM))
        assert spec["method"] == "GET"
        assert spec["params"]["url"] == probe.NEED_PARAMS["pdf ocr"]["url"]
        # a bare {"type": "integer"} is a schema node, not a usable value
        assert "pages" not in spec["params"]

    def test_falls_back_to_need_guess_without_a_spec(self):
        spec = probe.call_spec(cand(need="token price"))
        assert spec["params"] == probe.NEED_PARAMS["token price"]
        assert spec["source"] == "need_guess"

    def test_bare_need_string_still_works(self):
        assert probe.params_for("token price") == probe.NEED_PARAMS["token price"]
        assert probe.params_for(None) == {}


class TestPaidCoverage:
    def test_needs_are_round_robined_not_alphabetical(self):
        # 8 needs × top_k 3 against a 15-probe cap: alphabetical order gave the
        # first five needs every slot and "web search" none at all (gap 5).
        ranked = {n: [cand(url=f"https://{n}.com/{i}", pay_to=f"0x{n}{i}")
                      for i in range(3)]
                  for n in ("a need", "b need", "c need", "d need", "e need",
                            "f need", "g need", "web search")}
        sel = probe.select_candidates(ranked, max_paid=8, top_k=3)
        assert {c["need"] for c in sel["t1"]} == set(ranked)   # every need paid
        assert any(c["need"] == "web search" for c in sel["t1"])

    def test_price_ceiling_keeps_one_endpoint_from_eating_the_cap(self):
        # A single $0.25 probe took half the $0.50 run cap on 2026-07-27.
        ranked = {"n": [cand(url="https://premium.com/ask", pay_to="0x1", price="0.25"),
                        cand(url="https://cheap.com/t", pay_to="0x2", price="0.01")]}
        sel = probe.select_candidates(ranked, max_probe_usd=Decimal("0.05"))
        assert [c["url"] for c in sel["t1"]] == ["https://cheap.com/t"]
        # ...and it is REPORTED, not silently dropped
        assert [c["url"] for c in sel["too_expensive"]] == ["https://premium.com/ask"]
        assert len(sel["t0"]) == 2      # still gets the free liveness check

    def test_unpriced_candidate_is_not_ceiling_blocked(self):
        ranked = {"n": [cand(url="https://x.com/t", pay_to="0x1")]}
        ranked["n"][0]["price_usd"] = None
        sel = probe.select_candidates(ranked, max_probe_usd=Decimal("0.05"))
        assert len(sel["t1"]) == 1


class TestRetestQueue:
    @staticmethod
    def row(url, n, rate, **kw):
        return {"resource_url": url, "name": url, "paid_probes": n,
                "delivery_rate": rate, "need": "pdf ocr", **kw}

    def test_provisional_failures_come_first(self):
        q = probe.retest_queue([
            self.row("https://confirmed-fail/x", 3, 0.0),
            self.row("https://provisional-ok/x", 1, 1.0),
            self.row("https://provisional-fail/x", 1, 0.0),
        ])
        assert [c["url"] for c in q] == ["https://provisional-fail/x",
                                         "https://provisional-ok/x",
                                         "https://confirmed-fail/x"]

    def test_confirmed_winners_are_not_requeued(self):
        q = probe.retest_queue([self.row("https://proven/x", 4, 1.0)])
        assert q == []

    def test_unprobed_services_are_not_requeued(self):
        q = probe.retest_queue([self.row("https://never/x", 0, None)])
        assert q == []

    def test_retest_is_probed_before_fresh_candidates(self):
        # One-strike-and-rotate was the bug: "PDF to Text" failed once on
        # 07-20 and was never probed again (gap 3).
        ranked = {"pdf ocr": [cand(url="https://fresh.com/t", pay_to="0xf")]}
        retest = probe.retest_queue([self.row("https://failed-once.com/t", 1, 0.0)])
        sel = probe.select_candidates(ranked, retest=retest, max_paid=1)
        assert [c["url"] for c in sel["t1"]] == ["https://failed-once.com/t"]

    def test_retest_inherits_todays_advertised_call_shape(self):
        # A score row knows the URL but not how to call it; today's ranking does.
        spec = {"method": "GET", "queryParams": {"url": "https://e.com/f.pdf"}}
        ranked = {"pdf ocr": [cand(url="https://x.com/t", pay_to="0x1",
                                   input_spec=spec)]}
        retest = probe.retest_queue([self.row("https://x.com/t", 1, 0.0)])
        sel = probe.select_candidates(ranked, retest=retest)
        assert sel["t1"][0]["input_spec"] == spec


class TestFlagMatchesTheData:
    """The flag has to use the SAME definition of delivery as delivery_rate.
    Four services sat at 0.0 while every sweep reported '0 flagged' (gap 4)."""

    def test_settled_200_with_empty_body_is_a_non_delivery(self):
        p = probe_row(settle_ok=True, http_ok=True, response_nonempty=False)
        assert probe.took_payment_no_delivery(p) is True
        assert probe.paid_but_no_data(True, True) is False   # the old, narrow test

    def test_flag_fires_on_repeated_empty_deliveries(self):
        # X (Twitter) JSON API: 5 paid probes, delivery_rate 0.0, zero flags.
        rows = [probe_row(days_ago=d, http_ok=True, response_nonempty=False)
                for d in (1, 2, 3)]
        s = probe.score(rows, now=NOW)[0]
        assert s["delivery_rate"] == 0.0
        assert probe.FLAG_NO_DELIVERY in s["flags"]
        assert s["no_delivery_probes"] == 3

    def test_single_failure_stays_unconfirmed(self):
        s = probe.score([probe_row(http_ok=True, response_nonempty=False)],
                        now=NOW)[0]
        assert s["flags"] == [probe.FLAG_NO_DELIVERY_UNCONFIRMED]

    def test_schema_mismatch_after_payment_counts_as_no_delivery(self):
        p = probe_row(settle_ok=True, http_ok=True, response_nonempty=True,
                      schema_ok=False)
        assert probe.took_payment_no_delivery(p) is True

    def test_a_delivering_service_is_never_flagged(self):
        rows = [probe_row(days_ago=d) for d in (1, 2)]
        assert probe.score(rows, now=NOW)[0]["flags"] == []


class TestConfidence:
    def test_one_probe_is_provisional_both_ways(self):
        ok = probe.score([probe_row()], now=NOW)[0]
        assert ok["confidence"] == "provisional"
        assert ok["delivery_factor"] == probe.FACTOR_PROVISIONAL_GOOD

        bad = probe.score([probe_row(http_ok=False)], now=NOW)[0]
        assert bad["confidence"] == "provisional"
        # downranked, but not buried on a single data point
        assert bad["delivery_factor"] == probe.FACTOR_PROVISIONAL_BAD

    def test_two_probes_confirm_the_verdict(self):
        rows = [probe_row(days_ago=d) for d in (1, 2)]
        s = probe.score(rows, now=NOW)[0]
        assert s["confidence"] == "confirmed"
        assert s["delivery_factor"] == probe.FACTOR_GOOD

        rows = [probe_row(days_ago=d, http_ok=False) for d in (1, 2)]
        s = probe.score(rows, now=NOW)[0]
        assert s["confidence"] == "confirmed"
        assert s["delivery_factor"] == probe.FACTOR_BAD

    def test_unprobed_is_neutral_with_no_confidence(self):
        s = probe.score([probe_row(probe_type="free")], now=NOW)[0]
        assert s["confidence"] is None
        assert s["delivery_factor"] == probe.FACTOR_UNPROBED


class TestNeedLeaderboard:
    def test_head_to_head_within_a_need(self):
        rows = probe.score(
            [probe_row("https://good.com/t", days_ago=d) for d in (1, 2)] +
            [probe_row("https://bad.com/t", days_ago=d, http_ok=False)
             for d in (1, 2)],
            now=NOW)
        for r in rows:                       # score() carries need through
            r["need"] = "pdf ocr"
        board = probe.need_leaderboard(rows)
        assert [r["resource_url"] for r in board["pdf ocr"]] == \
               ["https://good.com/t", "https://bad.com/t"]

    def test_confirmed_outranks_provisional_at_equal_rate(self):
        rows = probe.score(
            [probe_row("https://twice.com/t", days_ago=d) for d in (1, 2)] +
            [probe_row("https://once.com/t")], now=NOW)
        for r in rows:
            r["need"] = "news"
        assert [r["confidence"] for r in probe.need_leaderboard(rows)["news"]] == \
               ["confirmed", "provisional"]

    def test_unprobed_services_are_absent_not_last(self):
        rows = probe.score([probe_row(probe_type="free")], now=NOW)
        assert probe.need_leaderboard(rows) == {}


class TestRailDetection:
    """AGE-83 gap 6: mpp_options: 0 is a real read (verified 2026-07-27 against
    96 live listings / 229 options — nobody advertises MPP or Tempo). The USDG
    label, though, was structurally dead: it looked for eip155:46630 while
    every live USDG listing advertises eip155:4663."""

    USDG_402 = {"accepts": [{
        "scheme": "exact", "network": "eip155:4663", "amount": "50000",
        "payTo": "0x50ab", "asset": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "extra": {"name": "Global Dollar", "version": "1"}}]}

    def test_live_usdg_option_is_detected(self):
        assert probe.t0_checks(402, self.USDG_402)["usdg_option"] is True

    def test_asset_name_alone_is_enough(self):
        opt = {"network": "eip155:1", "amount": "1000",
               "extra": {"name": "Global Dollar"}}
        assert probe.t0_checks(402, {"accepts": [opt]})["usdg_option"] is True

    def test_plain_usdc_on_base_is_not_usdg(self):
        r = probe.t0_checks(402, WELLFORMED_402)
        assert r["usdg_option"] is False and r["mpp_option"] is False


class TestOutputKeysDeliveryCheck:
    """rank()'s projection dropped the advertised response shape, so a paid
    call answering {"error": "..."} with HTTP 200 counted as DELIVERED — it
    was merely non-empty. AGE-83."""

    KEYS = ["url", "pages", "word_count", "text"]

    def test_error_body_fails_the_advertised_shape(self):
        r = probe.t1_evaluate({"error": "bad request"}, self.KEYS)
        assert r["response_nonempty"] is True
        assert r["schema_ok"] is False

    def test_real_payload_passes(self):
        r = probe.t1_evaluate({"url": "u", "pages": 3, "text": "hi"}, self.KEYS)
        assert r["schema_ok"] is True

    def test_no_advertised_keys_still_skips_rather_than_penalises(self):
        assert probe.t1_evaluate({"anything": 1}, [])["schema_ok"] is None
        assert probe.t1_evaluate({"anything": 1}, None)["schema_ok"] is None


class TestRetestDedup:
    def test_a_retest_row_without_pay_to_is_not_probed_twice(self):
        # /scores.json publishes no pay_to, so the retest row's dedup key was
        # (host, "") while today's ranking had (host, "0xabc") — the sweep paid
        # DeepSeek twice in one run (AGE-83 dry run, 2026-07-27).
        ranked = {"llm inference": [cand(url="https://api.deepseek/x402",
                                         pay_to="0xabc")]}
        retest = [{"url": "https://api.deepseek/x402", "name": "DeepSeek",
                   "pay_to": "", "need": "llm inference", "retest": True}]
        sel = probe.select_candidates(ranked, retest=retest)
        assert len(sel["t1"]) == 1
        assert len(sel["t0"]) == 1
        assert sel["t1"][0]["pay_to"] == "0xabc"   # enriched from the ranking


# ── AGE-86: only settled payments are delivery evidence ────────────────────────

class TestSettledOnlyScoring:
    """delivery_rate is a claim about what happens AFTER money moves. Before
    this gate, X (Twitter) JSON API sat publicly at 0.25× "confirmed" over six
    probes in which no payment was ever transmitted (its host didn't resolve)."""

    def test_x_twitter_regression_unsettled_rows_never_score(self):
        # The exact live case: 6 paid-type rows, none settled.
        rows = probe.score(
            [probe_row("https://x.1x402.sh/api/:name", days_ago=d,
                       settle_ok=False, http_ok=False, response_nonempty=False)
             for d in range(6)], now=NOW)
        r = rows[0]
        assert r["paid_probes"] == 0
        assert r["delivery_rate"] is None
        assert r["delivery_factor"] == 1.0        # unprobed-neutral, not 0.25
        assert r["confidence"] is None
        assert r["settle_failures"] == 6          # visible as what it is
        assert r["flags"] == []
        assert r["last_fail_at"] is None          # not the seller's failure

    def test_settle_failures_never_dilute_a_good_rate(self):
        rows = probe.score(
            [probe_row(days_ago=1), probe_row(days_ago=2)] +
            [probe_row(days_ago=3, settle_ok=False, http_ok=False,
                       response_nonempty=False)], now=NOW)
        r = rows[0]
        assert r["paid_probes"] == 2
        assert r["delivery_rate"] == 1.0          # 2/2 settled, not 2/3
        assert r["delivery_factor"] == probe.FACTOR_GOOD
        assert r["settle_failures"] == 1

    def test_flag_requires_settled_evidence(self):
        # Unsettled failures can NEVER produce the public accusation.
        rows = probe.score(
            [probe_row(days_ago=d, settle_ok=False, http_ok=False,
                       response_nonempty=False) for d in (1, 2, 3)], now=NOW)
        assert rows[0]["flags"] == []
        # ...but genuine settled non-deliveries still confirm it.
        rows = probe.score(
            [probe_row(days_ago=d, settle_ok=True, http_ok=False,
                       response_nonempty=False) for d in (1, 2)], now=NOW)
        assert probe.FLAG_NO_DELIVERY in rows[0]["flags"]
        assert rows[0]["no_delivery_probes"] == 2

    def test_latency_comes_from_settled_probes_only(self):
        # A 14ms DNS failure must not become the service's p50.
        rows = probe.score([
            probe_row(days_ago=1, latency_ms=4000),
            probe_row(days_ago=2, settle_ok=False, http_ok=False,
                      response_nonempty=False, latency_ms=14),
        ], now=NOW)
        assert rows[0]["latency_p50_ms"] == 4000


class TestProbePaidClassification:
    """AGE-86 in run.py: every T1 outcome is classified, and only verifiable
    settles produce scoreable rows. Fail closed — no proof of payment, no
    claim about the seller."""

    class _Session:
        def __init__(self, exc=None, result=None):
            self._exc, self._result = exc, result
        def would_exceed(self, price):
            return False
        def call(self, url, params):
            self.called_url, self.called_params = url, params
            if self._exc:
                raise self._exc
            return self._result

    @staticmethod
    def _cand(**kw):
        c = cand(url=kw.pop("url", "https://svc.example/tool"))
        c.update(kw)
        return c

    def test_dns_failure_is_unreachable_not_scored(self):
        # The X (Twitter) case: PrePaymentError before any 402.
        from agentpay import PrePaymentError
        from agents.prober import run as prober_run
        row = prober_run.probe_paid(self._Session(
            exc=PrePaymentError("External x402 call failed: [Errno -2] "
                                "Name or service not known")), self._cand())
        assert row["skipped"] is True
        assert row["outcome"] == "unreachable"

    def test_post_transmission_5xx_is_payment_rejected_not_scored(self):
        # The DeepSeek case: 502 wrapping the 400 WE caused. The old guard
        # matched only "spend recorded): 4" and scored this 0.0.
        from agents.prober import run as prober_run
        row = prober_run.probe_paid(self._Session(
            exc=Exception("External x402 call rejected after auth transmission "
                          "(settlement uncertain, spend recorded): 502 "
                          '{"error":"upstream_error"}')), self._cand())
        assert row["skipped"] is True
        assert row["outcome"] == "payment_rejected"

    def test_settle_failure_is_classified_not_scored(self):
        from agentpay import PaymentFailed
        from agents.prober import run as prober_run
        row = prober_run.probe_paid(self._Session(
            exc=PaymentFailed("insufficient balance")), self._cand())
        assert row["skipped"] is True
        assert row["outcome"] == "settle_failed"

    def test_refund_pending_stays_scoreable(self):
        # Money provably moved and nothing came back — THE flag case.
        from agentpay import RefundPending
        from agents.prober import run as prober_run
        row = prober_run.probe_paid(self._Session(
            exc=RefundPending("no result")), self._cand())
        assert not row.get("skipped")
        assert row["settle_ok"] is True and row["http_ok"] is False

    def test_success_is_settled_and_scoreable(self):
        from agents.prober import run as prober_run
        row = prober_run.probe_paid(self._Session(
            result={"text": "data"}), self._cand())
        assert not row.get("skipped")
        assert row["outcome"] == "settled"
        assert row["settle_ok"] is True

    def test_unresolvable_path_template_is_never_paid(self):
        from agents.prober import run as prober_run
        s = self._Session(result={"x": 1})
        row = prober_run.probe_paid(s, self._cand(
            url="https://api.gocreativeai.com/v1/twitter/tweets/user/:var1"))
        assert row["skipped"] is True
        assert row["outcome"] == "unfilled_path_template"
        assert not hasattr(s, "called_url")      # no money was risked

    def test_known_path_template_is_filled_before_paying(self):
        from agents.prober import run as prober_run
        s = self._Session(result={"x": 1})
        prober_run.probe_paid(s, self._cand(url="https://x.1x402.sh/api/:name"))
        assert s.called_url == "https://x.1x402.sh/api/coinbase"


# ── AGE-87: send what the seller published ─────────────────────────────────────

class TestSellerExampleWins:
    """The seller's published example is the one request shape they tested.
    AGE-83 had precedence backwards and burned 5 of 8 wasted settles in one
    sweep on values the listing contradicted."""

    def test_deepseek_regression_seller_model_wins(self):
        spec = {"method": "POST", "bodyType": "json", "body": {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "What is the capital of France?"}]}}
        s = probe.call_spec(cand(need="llm inference", input_spec=spec))
        assert s["params"]["model"] == "deepseek-v4-flash"     # not "default"
        assert s["params"]["messages"][0]["content"] == "What is the capital of France?"

    def test_otto_regression_seller_symbol_wins(self):
        spec = {"method": "GET", "queryParams": {"symbol": "BTC"}}
        s = probe.call_spec(cand(need="token price", input_spec=spec))
        assert s["params"]["symbol"] == "BTC"                  # not "ETH"

    def test_placeholder_example_falls_back_to_our_value(self):
        # example.com is a stand-in, not a working input — our real PDF wins.
        spec = {"method": "GET",
                "queryParams": {"url": "https://example.com/file.pdf"}}
        s = probe.call_spec(cand(need="pdf ocr", input_spec=spec))
        assert s["params"]["url"] == probe.NEED_PARAMS["pdf ocr"]["url"]

    def test_atlas_regression_declared_method_no_fields_sends_nothing(self):
        # {"method": "GET"} bare is an instruction: call me with nothing.
        # Atlas delivered 3/3 to a bare GET and 4xx'd when we appended an
        # uninvited ?symbol=BTC&token=BTC.
        s = probe.call_spec(cand(need="market data",
                                 input_spec={"method": "GET", "type": "http"}))
        assert s["params"] == {}
        assert s["source"] == "advertised_empty"

    def test_no_spec_at_all_still_uses_the_need_guess(self):
        s = probe.call_spec(cand(need="market data"))
        assert s["params"] == probe.NEED_PARAMS["market data"]
        assert s["source"] == "need_guess"


class TestFillPathTemplate:
    def test_plain_url_unchanged(self):
        assert probe.fill_path_template("https://a.com/t?x=1") == "https://a.com/t?x=1"

    def test_known_params_filled(self):
        assert probe.fill_path_template("https://x.1x402.sh/api/:name") == \
            "https://x.1x402.sh/api/coinbase"
        assert probe.fill_path_template("https://e.rip/token/:address").endswith(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        assert probe.fill_path_template("https://l.ge.com/v1/llm/:model").endswith(
            "/gpt-4o-mini")

    def test_brace_style_filled(self):
        assert probe.fill_path_template("https://a.com/u/{handle}/x") == \
            "https://a.com/u/coinbase/x"

    def test_unresolvable_param_returns_none(self):
        assert probe.fill_path_template(
            "https://api.gocreativeai.com/v1/twitter/tweets/user/:var1") is None

    def test_port_is_not_a_template(self):
        assert probe.fill_path_template("https://a.com:8443/t") == \
            "https://a.com:8443/t"


# ── AGE-88: on-chain reconciliation of uncertain settlements ───────────────────

class TestReconcileSettlements:
    ENTRIES = [
        {"tool": "https://ok.example/a", "cost": "$0.05", "state": "settled",
         "success": True},
        {"tool": "https://maybe.example/b", "cost": "$0.05",
         "state": "uncertain_settlement"},
        {"tool": "https://never.example/c", "cost": "$0.01",
         "state": "uncertain_settlement"},
    ]

    def test_settled_but_rejected_is_detected(self):
        # TWO 0.05 transfers on-chain: the confirmed one anchors the first,
        # so the uncertain $0.05 entry provably settled too.
        transfers = [{"to": "0xA", "value": "50000", "hash": "0xaaa"},
                     {"to": "0xB", "value": "50000", "hash": "0xbbb"}]
        r = probe.reconcile_settlements(self.ENTRIES, transfers)
        assert r[0]["resolution"] == "confirmed"
        assert r[1]["resolution"] == "settled_on_chain"     # took the money
        assert r[1]["tx_hash"] == "0xbbb"
        assert r[2]["resolution"] == "no_onchain_evidence"

    def test_confirmed_settle_cannot_lend_its_evidence(self):
        # Only ONE 0.05 transfer — it belongs to the confirmed entry; the
        # uncertain one must not borrow it.
        transfers = [{"to": "0xA", "value": "50000", "hash": "0xaaa"}]
        r = probe.reconcile_settlements(self.ENTRIES, transfers)
        assert r[0]["resolution"] == "confirmed"
        assert r[1]["resolution"] == "no_onchain_evidence"

    def test_no_transfers_no_evidence(self):
        r = probe.reconcile_settlements(self.ENTRIES, [])
        assert [x["resolution"] for x in r] == \
            ["confirmed", "no_onchain_evidence", "no_onchain_evidence"]
