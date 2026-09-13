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


def _limited_app():
    """The app wired the way main.py wires it, so the limiter's real behaviour
    is what gets asserted."""
    from fastapi import FastAPI
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    from gateway._limiter import limiter

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(infra.router)
    return TestClient(app)


def test_health_is_exempt_from_the_global_rate_limit():
    """The limiter keys on request.client.host, which behind a platform proxy is
    one bucket for every caller — so an unrelated burst would 429 the healthcheck
    and get a healthy process restarted."""
    client = _limited_app()
    codes = {client.get("/health").status_code for _ in range(260)}
    assert codes == {200}


def test_undecorated_public_reads_still_carry_the_default_limit():
    """The other half: the middleware exists so undecorated public routes are not
    unlimited. If this stops failing, the default limit has stopped applying."""
    client = _limited_app()
    codes = [client.get("/stats").status_code for _ in range(210)]
    assert 429 in codes


def test_routes_with_their_own_limit_are_left_to_the_decorator():
    """A decorated route must not also take the default: slowapi exempts it by
    module.name, which only holds while the decorator preserves both."""
    from fastapi import FastAPI
    from slowapi.middleware import (_find_route_handler, _get_route_name,
                                    _should_exempt)

    from gateway._limiter import limiter
    from gateway.routes import ledger, plan, prober, tools

    app = FastAPI()
    app.state.limiter = limiter
    for module in (infra, ledger, plan, prober, tools):
        app.include_router(module.router)

    for method, path in [("POST", "/tools/token_price/call"),
                         ("GET", "/ledger.json"),
                         ("POST", "/v1/flagship/run"),
                         ("POST", "/v1/prober/run"),
                         ("POST", "/v1/plan/estimate")]:
        scope = {"type": "http", "method": method, "path": path, "headers": [],
                 "root_path": "", "app": app, "query_string": b""}
        handler = _find_route_handler(app.routes, scope)
        assert handler is not None, f"{method} {path} did not resolve"
        assert _should_exempt(limiter, handler), (
            f"{method} {path} would take the default limit on top of its own "
            f"({_get_route_name(handler)} missing from the limiter's routes)"
        )
