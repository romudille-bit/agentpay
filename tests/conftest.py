"""
conftest.py — Shared pytest fixtures for the AgentPay test suite.

Three fixtures, in order of complexity:

  - clear_x402_state (autouse):
        x402.py keeps _pending_challenges and _completed_payments at
        module level. Tests that issue or verify challenges mutate that
        shared state; without a reset between tests, a leftover from one
        test corrupts the next. Runs automatically before every test.

  - mock_settings:
        Sets GATEWAY_PUBLIC_KEY to a known value so issue_payment_challenge
        produces deterministic challenges. Also disables Supabase + the
        keepalive loop (relevant when fixtures load gateway.main).

  - client:
        FastAPI TestClient over gateway.main:app, used by integration
        tests that exercise HTTP routes end-to-end. Triggers app startup
        + lifespan with KEEPALIVE_DISABLED=1 so the background ping never
        fires during tests.
"""

import os
import pytest


@pytest.fixture(autouse=True)
def clear_x402_state():
    """Reset module-level state in gateway.x402 before every test.

    Without this, _pending_challenges and _completed_payments from one
    test leak into the next, producing non-deterministic ordering bugs.
    """
    from gateway import x402
    x402._pending_challenges.clear()
    x402._completed_payments.clear()
    yield
    # Post-test cleanup is also automatic via the next test's autouse run,
    # but doing it here makes intent explicit and bounds memory if a test
    # populates large state.
    x402._pending_challenges.clear()
    x402._completed_payments.clear()


@pytest.fixture
def mock_settings(monkeypatch):
    """Force a known gateway public key + disable side-effecting integrations.

    Tests that call issue_payment_challenge() depend on
    settings.GATEWAY_PUBLIC_KEY being non-empty. In CI / fresh dev
    environments without a real .env, the default is "". This fixture
    sets a deterministic test value that callers can assert against.
    """
    test_gateway_pk = "GTESTGATEWAYPUBLICKEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    monkeypatch.setenv("GATEWAY_PUBLIC_KEY", test_gateway_pk)
    monkeypatch.setenv("KEEPALIVE_DISABLED", "1")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_KEY", "")
    # Tier 2 #18 changed STELLAR_FACILITATOR_ENABLED to default False.
    # Force-enable for existing OZ-flow tests; the disabled path has its own
    # dedicated test class (TestFacilitatorDisabled) which overrides this.
    monkeypatch.setenv("STELLAR_FACILITATOR_ENABLED", "true")

    # Reload settings so the new env values take effect. settings is
    # cached via @lru_cache in gateway.config, so we must reach in and
    # invalidate.
    from gateway.config import get_settings
    get_settings.cache_clear()
    new_settings = get_settings()

    # gateway/x402.py, /stellar.py, /base.py do `from gateway.config
    # import settings` at module-load time, capturing the OLD settings
    # reference. Re-binding via cache_clear() doesn't reach those modules.
    # Patch the module-level `settings` attribute on each so tests see the
    # new values everywhere settings is read.
    import gateway.x402
    monkeypatch.setattr(gateway.x402, "settings", new_settings)
    # stellar.py and base.py also import settings; patch them too if
    # they're loaded (which they are once x402 is imported, since x402
    # imports verify_payment from stellar).
    import gateway.stellar
    if hasattr(gateway.stellar, "settings"):
        monkeypatch.setattr(gateway.stellar, "settings", new_settings)
    import gateway.base
    if hasattr(gateway.base, "settings"):
        monkeypatch.setattr(gateway.base, "settings", new_settings)

    yield new_settings
    get_settings.cache_clear()


@pytest.fixture
def client(mock_settings):
    """FastAPI TestClient with the test settings applied.

    Use this for integration tests that hit HTTP routes. Importing
    gateway.main triggers @app.on_event("startup"), so the env vars
    set by mock_settings (KEEPALIVE_DISABLED=1, SUPABASE blanked) must
    be in place before this fixture runs.
    """
    from fastapi.testclient import TestClient
    from gateway.main import app
    with TestClient(app) as c:
        yield c
