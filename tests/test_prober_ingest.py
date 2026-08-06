"""tests/test_prober_ingest.py — POST /v1/prober/run (AGE-6).

Route tests via TestClient with the Supabase helpers monkeypatched (same
convention as test_ledger.py's ingest tests). Verifies the gate, the payload
contract, the store-then-rescore-over-window flow, and the best-effort 202.
"""

from fastapi.testclient import TestClient

from gateway.config import settings
from gateway.routes import prober


SECRET_HDR = {"X-Flagship-Secret": "s3cr3t"}


def _paid_probe(url="https://api.example.com/tools/x", **kw):
    row = {
        "probed_at": "2026-07-10T12:00:00+00:00",
        "resource_url": url,
        "pay_to": "0xabc", "network": "eip155:8453", "price_usdc": "0.01",
        "probe_type": "paid",
        "settle_ok": True, "http_ok": True,
        "response_nonempty": True, "schema_ok": None, "latency_ms": 700,
        "tx_hash": "0xdead", "error": None,
    }
    row.update(kw)
    return row


def _patch_store(monkeypatch, *, window=None, probes_ok=True, scores_ok=True,
                 run_ok=True):
    calls = {"probes": None, "scores": None, "run": None}

    async def _insert_probes(rows):
        calls["probes"] = rows
        return probes_ok

    async def _fetch_window(window_days=30, limit=5000):
        return list(window or [])

    async def _upsert_scores(rows):
        calls["scores"] = rows
        return scores_ok

    async def _insert_run(run):
        calls["run"] = run
        return run_ok

    monkeypatch.setattr(prober, "insert_service_probes", _insert_probes)
    monkeypatch.setattr(prober, "fetch_service_probes", _fetch_window)
    monkeypatch.setattr(prober, "upsert_service_scores", _upsert_scores)
    monkeypatch.setattr(prober, "insert_flagship_run", _insert_run)
    return calls


def _client():
    from gateway.main import app
    return TestClient(app)


# ── gate ──────────────────────────────────────────────────────────────────────

def test_404_when_secret_unset(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "")
    assert _client().post("/v1/prober/run", json={"probes": []}).status_code == 404


def test_401_on_bad_secret(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    r = _client().post("/v1/prober/run", json={"probes": []},
                       headers={"X-Flagship-Secret": "wrong"})
    assert r.status_code == 401


def test_400_without_probes_list(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    r = _client().post("/v1/prober/run", json={"run": {}}, headers=SECRET_HDR)
    assert r.status_code == 400


# ── store + rescore ───────────────────────────────────────────────────────────

def test_stores_probes_and_rescoreds_window(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    # Window returns history: same service probed before, once failed.
    history = [
        _paid_probe(),
        _paid_probe(probed_at="2026-07-08T12:00:00+00:00",
                    settle_ok=True, http_ok=False, response_nonempty=False),
    ]
    calls = _patch_store(monkeypatch, window=history)

    r = _client().post("/v1/prober/run",
                       json={"probes": [_paid_probe()],
                             "run": {"run_at_iso": "2026-07-10T12:01:00+00:00"}},
                       headers=SECRET_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["probes_stored"] and body["scores_stored"] and body["run_stored"]
    assert body["window_rows"] == 2
    # Scores computed over the WINDOW (2 paid probes → rate 0.5), not just
    # this run's single clean probe.
    assert len(body["scores"]) == 1
    s = body["scores"][0]
    assert s["paid_probes"] == 2
    assert s["delivery_rate"] == 0.5
    # 1 no-delivery in the window → unconfirmed (AGE-11 split policy)
    assert "no_delivery_unconfirmed" in s["flags"]
    # And the upsert got the same rows.
    assert calls["scores"][0]["delivery_rate"] == 0.5


def test_scores_this_run_when_window_unreadable(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    _patch_store(monkeypatch, window=[])  # fetch returns [] (table missing/blip)
    r = _client().post("/v1/prober/run", json={"probes": [_paid_probe()]},
                       headers=SECRET_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["window_rows"] == 0
    assert len(body["scores"]) == 1
    assert body["scores"][0]["paid_probes"] == 1     # fell back to run rows
    # AGE-83: one paid probe earns the PROVISIONAL boost, not the full 1.15 —
    # succeed-twice-to-trust.
    assert body["scores"][0]["delivery_factor"] == 1.05
    assert body["scores"][0]["confidence"] == "provisional"


def test_202_when_storage_partial(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    _patch_store(monkeypatch, probes_ok=False)
    r = _client().post("/v1/prober/run", json={"probes": [_paid_probe()]},
                       headers=SECRET_HDR)
    assert r.status_code == 202
    assert r.json()["probes_stored"] is False


def test_run_summary_optional(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    calls = _patch_store(monkeypatch)
    r = _client().post("/v1/prober/run", json={"probes": [_paid_probe()]},
                       headers=SECRET_HDR)
    assert r.status_code == 200            # no run posted → still fully stored
    assert r.json()["run_stored"] is False
    assert calls["run"] is None


def test_non_dict_probe_rows_are_skipped(monkeypatch):
    monkeypatch.setattr(settings, "FLAGSHIP_INGEST_SECRET", "s3cr3t")
    calls = _patch_store(monkeypatch)
    r = _client().post("/v1/prober/run",
                       json={"probes": [_paid_probe(), "junk", 42]},
                       headers=SECRET_HDR)
    assert r.status_code == 200
    assert len(calls["probes"]) == 1


# ── GET /scores.json (AGE-20 stage 1) ────────────────────────────────────────

def test_scores_json_public_shape(monkeypatch):
    async def _fake_scores():
        return {
            "https://good.x/t": {
                "resource_url": "https://good.x/t", "window_days": 30,
                "paid_probes": 2, "delivery_rate": 1.0, "delivery_factor": 1.15,
                "latency_p50_ms": 1667, "flags": [], "mpp_option": False,
                "usdg_option": False, "price_usdc": "0.02",
                "last_ok_at": "2026-07-10T18:40:32+00:00", "last_fail_at": None,
            },
            "https://dead.x/t": {
                "resource_url": "https://dead.x/t", "window_days": 30,
                "paid_probes": 1, "delivery_rate": 0.0, "delivery_factor": 0.25,
                "latency_p50_ms": 17, "flags": [], "mpp_option": False,
                "usdg_option": False, "price_usdc": "0.05",
                "last_ok_at": None, "last_fail_at": "2026-07-10T18:40:38+00:00",
            },
        }
    from gateway.services import supabase
    monkeypatch.setattr(prober, "fetch_service_scores", _fake_scores, raising=False)
    monkeypatch.setattr(supabase, "fetch_service_scores", _fake_scores)
    r = _client().get("/scores.json")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    # sorted by factor desc — the delivering service leads
    assert body["services"][0]["resource_url"] == "https://good.x/t"
    assert body["services"][0]["why"].startswith("probed 2×")
    assert body["services"][1]["delivery_factor"] == 0.25
    assert r.headers["cache-control"] == "no-store"


def test_scores_json_empty_ok(monkeypatch):
    async def _empty():
        return {}
    from gateway.services import supabase
    monkeypatch.setattr(supabase, "fetch_service_scores", _empty)
    r = _client().get("/scores.json")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_probes_page_serves_html():
    r = _client().get("/probes")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "x402 Delivery Scores" in r.text
    assert "/scores.json" in r.text          # fetches the JSON client-side
    assert r.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------- AGE-38
# amount_usdc is a TEXT column: PostgREST "gt.0" compares strings, so
# "0.000000" (free-flow receipts) sneaks past the server-side filter.
# _group_paid_receipts is the authoritative Python-side paid/free split.

def test_group_paid_receipts_drops_free_rows():
    from gateway.services.supabase import _group_paid_receipts
    rows = [  # newest-first, as the query orders
        {"tool_name": "pre_trade_check", "amount_usdc": "0.010000",
         "created_at": "2026-07-13T10:00:00Z"},
        {"tool_name": "fear_greed_index", "amount_usdc": "0.000000",
         "created_at": "2026-07-13T09:00:00Z"},          # free — must drop
        {"tool_name": "pre_trade_check", "amount_usdc": "0.01",
         "created_at": "2026-07-12T10:00:00Z"},
        {"tool_name": "token_price", "amount_usdc": "0",
         "created_at": "2026-07-12T09:00:00Z"},          # free — must drop
        {"tool_name": "verified_route", "amount_usdc": "0.010000",
         "created_at": "2026-06-30T10:00:00Z"},
        {"tool_name": "session_create", "amount_usdc": "garbage",
         "created_at": "2026-06-29T10:00:00Z"},          # unparseable — drop
        {"tool_name": "", "amount_usdc": "0.01",
         "created_at": "2026-06-28T10:00:00Z"},          # no tool — drop
    ]
    out = _group_paid_receipts(rows)
    assert [r["tool"] for r in out] == ["pre_trade_check", "verified_route"]
    assert out[0]["paid_calls"] == 2
    assert out[0]["last_paid_at"] == "2026-07-13T10:00:00Z"  # newest kept
    tools = {r["tool"] for r in out}
    assert "fear_greed_index" not in tools and "token_price" not in tools


def test_group_paid_receipts_empty():
    from gateway.services.supabase import _group_paid_receipts
    assert _group_paid_receipts([]) == []


def test_scores_json_own_tools_enriched(monkeypatch):
    """AGE-38 redesign: own paid tools carry the registry price; free tools
    surface as a count, never as rows mistakable for paid demand."""
    async def _empty_scores():
        return {}
    async def _own():
        return [{"tool": "pre_trade_check", "paid_calls": 31,
                 "last_paid_at": "2026-07-13T10:00:00+00:00"}]
    from gateway.services import supabase
    monkeypatch.setattr(supabase, "fetch_service_scores", _empty_scores)
    monkeypatch.setattr(supabase, "fetch_own_tool_receipts", _own)
    body = _client().get("/scores.json").json()
    own = body["own_tools"]
    assert own["tools"][0]["price_usdc"] == "0.01"
    assert own["free_tools"]["count"] == 17
    assert "no payment" in own["free_tools"]["note"]


def test_probes_page_self_section_copy():
    r = _client().get("/probes")
    assert "never self-scored" in r.text
    assert "real customers paying real USDC" in r.text
    assert "customer-paid calls" in r.text          # card caption, not jargon
    assert "Receipted paid calls" not in r.text     # old table header gone


# ---------------------------------------------------------------- AGE-39
# Per-service SEO pages: server-rendered /s/{slug} from service_scores.

_ROW = {
    "resource_url": "https://api.exa.ai/search", "name": "Exa Search",
    "need": "web search", "network": "eip155:8453", "window_days": 30,
    "paid_probes": 8, "delivery_rate": 1.0, "delivery_factor": 1.15,
    "latency_p50_ms": 740, "flags": [], "mpp_option": True,
    "usdg_option": False, "price_usdc": "0.01",
    "last_ok_at": "2026-07-10T18:40:32+00:00", "last_fail_at": None,
}


def _patch_scores(monkeypatch):
    async def _scores():
        return {_ROW["resource_url"]: dict(_ROW)}
    from gateway.services import supabase
    monkeypatch.setattr(supabase, "fetch_service_scores", _scores)


def test_service_slug_stable_readable_unique():
    s = prober.service_slug("https://api.exa.ai/search")
    assert s == prober.service_slug("https://api.exa.ai/search")  # stable
    assert s.startswith("api-exa-ai-search-") and len(s.split("-")[-1]) == 6
    assert s != prober.service_slug("https://api.exa.ai/search2")  # unique


def test_service_page_server_rendered(monkeypatch):
    _patch_scores(monkeypatch)
    slug = prober.service_slug(_ROW["resource_url"])
    r = _client().get(f"/s/{slug}")
    assert r.status_code == 200
    # SEO essentials are IN the HTML, not fetched client-side
    assert "Exa Search — x402 delivery score" in r.text
    assert f'rel="canonical" href="https://agentpay.tools/s/{slug}"' in r.text
    assert "100%" in r.text and "740ms" in r.text
    assert "also payable via MPP/Tempo" in r.text
    assert "verified_route" in r.text
    assert r.headers["cache-control"] == "public, max-age=300"


def test_service_page_unknown_404(monkeypatch):
    _patch_scores(monkeypatch)
    assert _client().get("/s/nope-000000").status_code == 404


def test_scores_json_rows_carry_page_link(monkeypatch):
    _patch_scores(monkeypatch)
    body = _client().get("/scores.json").json()
    assert body["services"][0]["page"] == \
        "/s/" + prober.service_slug(_ROW["resource_url"])


def test_sitemap_includes_service_pages(monkeypatch):
    _patch_scores(monkeypatch)
    r = _client().get("/sitemap.xml")
    assert "/s/" + prober.service_slug(_ROW["resource_url"]) in r.text


# ── AGE-104 follow-up: probe-coverage honesty on the Prober's own surfaces ────
# The "Base only — on-chain delivery unverified" caveat shipped on
# verified_route but NOT on scores.json or the /probes leaderboard, so three
# Solana services sat on our public delivery board with no indication we have
# never verified them and currently cannot settle their rail at all.

class TestProbeCoverageNote:
    def test_base_is_covered_and_carries_no_caveat(self):
        from gateway.radar import probe_coverage_note
        assert probe_coverage_note("eip155:8453") is None
        assert probe_coverage_note("base") is None          # alias
        assert probe_coverage_note("") is None              # nothing to say
        assert probe_coverage_note(None) is None

    def test_non_base_rails_are_flagged_unverified(self):
        from gateway.radar import PROBE_COVERAGE_UNVERIFIED, probe_coverage_note
        for net in ("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                    "solana", "eip155:42161", "eip155:137", "stellar:pubnet"):
            assert probe_coverage_note(net) == PROBE_COVERAGE_UNVERIFIED, net

    def test_legacy_lowercased_solana_id_is_recognized_and_normalized(self):
        """Rows written before the Solana aliases stored a LOWERCASED base58
        CAIP-2, which is not a valid identifier (base58 is case-sensitive).
        Both the caveat and the published network string must still be right."""
        from gateway.radar import (PROBE_COVERAGE_UNVERIFIED, normalize_network,
                                   probe_coverage_note)
        legacy = "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp"
        assert probe_coverage_note(legacy) == PROBE_COVERAGE_UNVERIFIED
        assert normalize_network(legacy) == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

    def test_verified_route_public_projection_uses_the_same_rule(self):
        """One helper, every surface — an Arbitrum listing gets the same caveat
        a Solana one does, so a new chain never needs a second code change."""
        from decimal import Decimal

        from gateway.radar import PROBE_COVERAGE_UNVERIFIED, _public
        def pub(net):
            return _public({"name": "x", "url": "u", "price_usd": Decimal("0.01"),
                            "network": net, "network_caip2": net, "pay_to": "",
                            "tags": [], "calls30d": 0, "payers30d": 0,
                            "quality": 0, "flags": []})
        assert pub("eip155:42161")["probe_coverage"] == PROBE_COVERAGE_UNVERIFIED
        assert "probe_coverage" not in pub("eip155:8453")


def test_scores_json_carries_coverage_caveat_and_normalized_network(monkeypatch):
    """End-to-end: a legacy Solana row (lowercased base58 id, never probed)
    must reach /scores.json with the honesty caveat AND a valid CAIP-2, while
    a Base row stays clean. This is the actual regression — the helper was
    right, but nothing wired it into the Prober's own published surface."""
    from gateway.radar import PROBE_COVERAGE_UNVERIFIED

    async def _fake_scores():
        return {
            "https://sol.x/t": {
                "resource_url": "https://sol.x/t", "window_days": 30,
                "network": "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp",
                "paid_probes": 0, "delivery_rate": None, "delivery_factor": 1.0,
                "latency_p50_ms": None, "flags": [], "price_usdc": "0.01",
            },
            "https://base.x/t": {
                "resource_url": "https://base.x/t", "window_days": 30,
                "network": "eip155:8453",
                "paid_probes": 3, "delivery_rate": 1.0, "delivery_factor": 1.15,
                "latency_p50_ms": 900, "flags": [], "price_usdc": "0.01",
            },
        }
    from gateway.services import supabase
    monkeypatch.setattr(prober, "fetch_service_scores", _fake_scores, raising=False)
    monkeypatch.setattr(supabase, "fetch_service_scores", _fake_scores)

    body = _client().get("/scores.json").json()
    rows = {s["resource_url"]: s for s in body["services"]}

    sol = rows["https://sol.x/t"]
    assert sol["probe_coverage"] == PROBE_COVERAGE_UNVERIFIED
    # the invalid lowercased id must not be republished
    assert sol["network"] == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

    base = rows["https://base.x/t"]
    assert base["probe_coverage"] is None
    assert base["network"] == "eip155:8453"
