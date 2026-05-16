"""
test_refund_worker.py — Tests for the PR #12 refund worker loop.

The worker itself is an infinite `while True` with asyncio.sleep, which
is awkward to unit-test directly. Instead we exercise its INNER block
— the per-row processing logic — by running a single iteration through
the same code path with mocked supabase + send_refund.

Pattern: factor the inner block into a helper isn't feasible without
restructuring main.py; instead, we drive the worker by patching
sleep to break the loop after one tick, the way pytest-asyncio
tests of long-running background tasks usually go.

Coverage:
  - REFUND_ENABLED=False → worker is NOT scheduled by lifespan (covered
    indirectly via the existing test_routes_tools.py lifecycle tests)
  - REFUND_ENABLED=True → worker processes pending rows: happy path,
    retry path, cap-exhaustion path, base-network short-circuit
"""

import asyncio
import pytest


@pytest.fixture
def mocked_refund_deps(monkeypatch):
    """Stub out sb.* and stellar.send_refund so we can drive
    _refund_worker_loop's inner logic deterministically. Returns a
    dict the test mutates to control behaviour + capture calls.
    """
    state = {
        "rows":       [],     # what claim_refund_pending returns
        "send_results": [],   # popped one per call to send_refund
        "attempts":   {},     # payment_id → increment count
        "done_calls": [],     # (payment_id, tx_hash) tuples
        "failed_calls": [],   # (payment_id, reason) tuples
    }

    async def fake_claim(limit=20):
        # Return the configured rows ONCE; subsequent calls return []
        # so the loop can be driven by a single tick without re-firing.
        if state["rows"]:
            r = state["rows"]
            state["rows"] = []
            return r
        return []

    async def fake_increment(payment_id):
        state["attempts"][payment_id] = state["attempts"].get(payment_id, 0) + 1

    async def fake_mark_done(payment_id, refund_tx_hash):
        state["done_calls"].append((payment_id, refund_tx_hash))

    async def fake_mark_failed(payment_id, reason):
        state["failed_calls"].append((payment_id, reason))

    async def fake_send_refund(agent_address, amount_usdc, payment_id):
        if state["send_results"]:
            return state["send_results"].pop(0)
        return {"success": True, "tx_hash": "default_hash"}

    import gateway.services.supabase as sb_mod
    monkeypatch.setattr(sb_mod, "claim_refund_pending", fake_claim)
    monkeypatch.setattr(sb_mod, "increment_refund_attempt", fake_increment)
    monkeypatch.setattr(sb_mod, "mark_refund_done", fake_mark_done)
    monkeypatch.setattr(sb_mod, "mark_refund_failed", fake_mark_failed)

    import gateway.stellar as stellar_mod
    monkeypatch.setattr(stellar_mod, "send_refund", fake_send_refund)

    # Also replace _refund_worker_loop's asyncio.sleep with a fast version
    # so the test doesn't actually wait 60s between ticks.
    import gateway.main as main_mod
    real_sleep = asyncio.sleep
    sleep_count = {"n": 0}
    async def fast_sleep(seconds):
        sleep_count["n"] += 1
        # After the second sleep call (initial + post-tick), cancel the
        # task so the loop exits cleanly.
        if sleep_count["n"] >= 2:
            raise asyncio.CancelledError()
        await real_sleep(0)
    monkeypatch.setattr(main_mod.asyncio, "sleep", fast_sleep)

    return state


@pytest.mark.asyncio
async def test_refund_worker_happy_path_marks_done(mocked_refund_deps):
    """One pending row, send_refund succeeds → mark_refund_done called."""
    mocked_refund_deps["rows"] = [{
        "payment_id":      "pid-happy",
        "agent_address":   "GAGENT",
        "amount_usdc":     "0.002",
        "network":         "stellar-testnet",
        "tool_name":       "token_price",
        "refund_attempts": 0,
    }]
    mocked_refund_deps["send_results"] = [
        {"success": True, "tx_hash": "happy_refund_tx"},
    ]

    from gateway.main import _refund_worker_loop
    with pytest.raises(asyncio.CancelledError):
        await _refund_worker_loop()

    assert mocked_refund_deps["attempts"]["pid-happy"] == 1
    assert mocked_refund_deps["done_calls"] == [("pid-happy", "happy_refund_tx")]
    assert mocked_refund_deps["failed_calls"] == []


@pytest.mark.asyncio
async def test_refund_worker_retry_does_not_mark_failed_until_cap(mocked_refund_deps):
    """A row on attempt 2/5 that fails again should NOT be marked
    refund_failed — the next sweep will retry it. Only the 5th
    consecutive failure flips it to refund_failed."""
    mocked_refund_deps["rows"] = [{
        "payment_id":      "pid-retry",
        "agent_address":   "GAGENT",
        "amount_usdc":     "0.001",
        "network":         "stellar-testnet",
        "tool_name":       "token_price",
        "refund_attempts": 2,  # 2 prior failures; this would be #3
    }]
    mocked_refund_deps["send_results"] = [
        {"success": False, "reason": "horizon_timeout"},
    ]

    from gateway.main import _refund_worker_loop
    with pytest.raises(asyncio.CancelledError):
        await _refund_worker_loop()

    assert mocked_refund_deps["attempts"]["pid-retry"] == 1
    # Not done, not failed — just retrying
    assert mocked_refund_deps["done_calls"] == []
    assert mocked_refund_deps["failed_calls"] == []


@pytest.mark.asyncio
async def test_refund_worker_cap_exhaustion_marks_failed(mocked_refund_deps):
    """A row on attempt 5/5 that fails should be marked refund_failed
    with the last error reason captured."""
    mocked_refund_deps["rows"] = [{
        "payment_id":      "pid-doomed",
        "agent_address":   "GAGENT",
        "amount_usdc":     "0.002",
        "network":         "stellar-testnet",
        "tool_name":       "token_price",
        "refund_attempts": 4,  # 4 prior failures; this would be #5 (cap)
    }]
    mocked_refund_deps["send_results"] = [
        {"success": False, "reason": "op_no_trust"},
    ]

    from gateway.main import _refund_worker_loop
    with pytest.raises(asyncio.CancelledError):
        await _refund_worker_loop()

    assert mocked_refund_deps["attempts"]["pid-doomed"] == 1
    assert mocked_refund_deps["done_calls"] == []
    # Failed with the last error reason embedded
    assert len(mocked_refund_deps["failed_calls"]) == 1
    pid, reason = mocked_refund_deps["failed_calls"][0]
    assert pid == "pid-doomed"
    assert "op_no_trust" in reason
    assert "max_attempts" in reason


@pytest.mark.asyncio
async def test_refund_worker_base_network_short_circuits(mocked_refund_deps):
    """Base refunds aren't implemented (no outgoing Base tx machinery).
    A refund_pending row with network='base-mainnet' must short-circuit
    to refund_failed WITHOUT calling send_refund — otherwise the row
    loops forever burning Stellar attempts that can't succeed."""
    mocked_refund_deps["rows"] = [{
        "payment_id":      "pid-base",
        "agent_address":   "0xabc...",
        "amount_usdc":     "0.002",
        "network":         "base-mainnet",
        "tool_name":       "funding_rates",
        "refund_attempts": 0,
    }]
    # Pre-load a send_refund result that, if called, would mark this
    # row as done — so the test fails if base short-circuit is missing.
    mocked_refund_deps["send_results"] = [
        {"success": True, "tx_hash": "would_be_wrong"},
    ]

    from gateway.main import _refund_worker_loop
    with pytest.raises(asyncio.CancelledError):
        await _refund_worker_loop()

    # send_refund was NOT called (the success_result was not popped)
    assert mocked_refund_deps["send_results"] == [
        {"success": True, "tx_hash": "would_be_wrong"},
    ]
    # increment_refund_attempt also skipped
    assert "pid-base" not in mocked_refund_deps["attempts"]
    # Marked failed with the right reason
    assert mocked_refund_deps["failed_calls"] == [
        ("pid-base", "base_refund_not_implemented"),
    ]
