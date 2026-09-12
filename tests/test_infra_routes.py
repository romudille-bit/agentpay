"""tests/test_infra_routes.py — /health and /stats.

/stats is unauthenticated and uncached, so what it publishes is a public feed.
The in-memory transaction log it reads from carries the buyer's wallet and the
settlement hash; those are the same fields the public ledger deliberately
withholds, and an address-to-tool feed is a customer list.
"""

from fastapi.testclient import TestClient

from gateway.routes import infra
from gateway.services.transaction_log import append_transaction


def _client() -> TestClient:
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(infra.router)
    return TestClient(app)


def test_stats_publishes_shape_not_identities():
    append_transaction({
        "tool": "pre_trade_check",
        "amount_usdc": "0.01",
        "agent": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "tx_hash": "0x" + "ab" * 32,
        "success": True,
    })
    body = _client().get("/stats").json()

    recent = body["recent_transactions"]
    assert recent, "the activity feed should still exist"
    published = set().union(*(set(row) for row in recent))
    assert published == {"tool", "amount_usdc", "success"}

    raw = _client().get("/stats").text
    assert "0xdeadbeef" not in raw
    assert "ab" * 32 not in raw


def test_health_reports_status_and_commit():
    body = _client().get("/health").json()
    assert body["status"] == "ok"
    assert "commit" in body


def test_stats_recent_activity_is_capped_at_ten():
    for i in range(15):
        append_transaction({
            "tool": f"t{i}", "amount_usdc": "0.01",
            "agent": "0x" + "f" * 40, "tx_hash": "0x" + "0" * 64, "success": True,
        })
    body = _client().get("/stats").json()
    assert len(body["recent_transactions"]) <= 10
