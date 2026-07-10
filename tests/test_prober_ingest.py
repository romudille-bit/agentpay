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
    assert "took_payment_no_delivery" in s["flags"]
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
    assert body["scores"][0]["delivery_factor"] == 1.15


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
