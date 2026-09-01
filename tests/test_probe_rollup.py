"""
Tests for gateway/services/probe_rollup.py — the batched 402/probe
telemetry that replaced per-event DB writes (disk-IO fix, 2026-08-04).
"""

import asyncio

import pytest

from gateway.services import probe_rollup


@pytest.fixture(autouse=True)
def clean_counts():
    probe_rollup._counts.clear()
    yield
    probe_rollup._counts.clear()


class TestRecord402:

    def test_counts_accumulate_per_key(self):
        probe_rollup.record_402("token_price", "axios/1.14.0", "free_402")
        probe_rollup.record_402("token_price", "axios/1.14.0", "free_402")
        probe_rollup.record_402("token_price", "node", "free_402")
        probe_rollup.record_402("pre_trade_check", "axios/1.14.0", "paid_402")

        totals = {}
        for (day, tool, ua, kind), n in probe_rollup._counts.items():
            totals[(tool, ua, kind)] = n
        assert totals[("token_price", "axios/1.14.0", "free_402")] == 2
        assert totals[("token_price", "node", "free_402")] == 1
        assert totals[("pre_trade_check", "axios/1.14.0", "paid_402")] == 1

    def test_none_user_agent_is_bucketed(self):
        probe_rollup.record_402("token_price", None, "probe_get")
        (day, tool, ua, kind), = probe_rollup._counts.keys()
        assert ua == "(none)"

    def test_long_user_agent_truncated(self):
        probe_rollup.record_402("token_price", "x" * 500, "probe_get")
        (day, tool, ua, kind), = probe_rollup._counts.keys()
        assert len(ua) == 160

    def test_key_overflow_collapses_into_bucket(self, monkeypatch):
        """A UA-randomizing scanner can't grow memory unboundedly: past
        _MAX_KEYS, new UAs collapse into the overflow bucket — totals stay
        accurate even if per-UA detail saturates."""
        monkeypatch.setattr(probe_rollup, "_MAX_KEYS", 3)
        for i in range(10):
            probe_rollup.record_402("token_price", f"scanner/{i}", "free_402")
        assert len(probe_rollup._counts) <= 4  # 3 real keys + overflow
        assert sum(probe_rollup._counts.values()) == 10
        overflow = [
            n for (day, tool, ua, kind), n in probe_rollup._counts.items()
            if ua == probe_rollup._OVERFLOW_UA
        ]
        assert overflow and overflow[0] == 7


class TestFlush:

    def test_flush_noop_when_supabase_disabled(self, monkeypatch):
        monkeypatch.setattr(probe_rollup.sb, "sb_enabled", lambda: False)
        probe_rollup.record_402("token_price", "node", "free_402")
        assert asyncio.run(probe_rollup.flush()) == 0
        # Counts retained for a later flush (e.g. Supabase comes back)
        assert sum(probe_rollup._counts.values()) == 1

    def test_flush_failure_requeues_counts(self, monkeypatch):
        """A failed flush must merge the snapshot back — telemetry is not
        silently dropped, and counts recorded DURING the failed flush are
        preserved too."""
        monkeypatch.setattr(probe_rollup.sb, "sb_enabled", lambda: True)
        monkeypatch.setattr(probe_rollup.sb, "sb_headers", lambda: {})

        class BoomClient:
            def __init__(self, *a, **kw): ...
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, *a, **kw):
                raise RuntimeError("supabase down")

        monkeypatch.setattr(probe_rollup.httpx, "AsyncClient", BoomClient)

        probe_rollup.record_402("token_price", "node", "free_402")
        probe_rollup.record_402("token_price", "node", "free_402")
        n = asyncio.run(probe_rollup.flush())
        assert n == 0
        assert sum(probe_rollup._counts.values()) == 2

    def test_flush_posts_one_batch(self, monkeypatch):
        monkeypatch.setattr(probe_rollup.sb, "sb_enabled", lambda: True)
        monkeypatch.setattr(probe_rollup.sb, "sb_headers", lambda: {})
        monkeypatch.setattr(
            probe_rollup.settings, "SUPABASE_URL", "https://sb.example", raising=False
        )

        posted = {"calls": 0, "rows": None}

        class OkResp:
            status_code = 201
            text = ""

        class CaptureClient:
            def __init__(self, *a, **kw): ...
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, headers=None, json=None):
                posted["calls"] += 1
                posted["rows"] = json
                return OkResp()

        monkeypatch.setattr(probe_rollup.httpx, "AsyncClient", CaptureClient)

        probe_rollup.record_402("token_price", "axios/1.14.0", "free_402")
        probe_rollup.record_402("token_price", "axios/1.14.0", "free_402")
        probe_rollup.record_402("gas_tracker", "node", "probe_get")

        n = asyncio.run(probe_rollup.flush())
        assert n == 2                      # two distinct keys → two rows
        assert posted["calls"] == 1        # ...in ONE batch POST
        assert probe_rollup._counts == {}  # drained on success

        by_key = {(r["tool_name"], r["user_agent"], r["state"]): r["n"]
                  for r in posted["rows"]}
        assert by_key[("token_price", "axios/1.14.0", "free_402")] == 2
        assert by_key[("gas_tracker", "node", "probe_get")] == 1
        assert all(set(r) == {"day", "tool_name", "user_agent", "state",
                              "network", "n"} for r in posted["rows"])


class TestHourlyRollupCadence:
    """Disk-IO fix #3 (2026-09-01): the rollup INSERT is hourly, not every
    5-min tick — additive rows made a 5-min cadence ~8,400 rows/day."""

    def test_rollup_interval_is_hourly_and_coarser_than_tick(self):
        assert probe_rollup.ROLLUP_FLUSH_INTERVAL_SECONDS == 3600
        assert probe_rollup.ROLLUP_FLUSH_INTERVAL_SECONDS > probe_rollup.FLUSH_INTERVAL_SECONDS

    def test_rollup_due_only_after_interval(self):
        due = probe_rollup._rollup_due
        assert not due(last_flush=0.0, now=0.0)
        assert not due(last_flush=0.0, now=probe_rollup.FLUSH_INTERVAL_SECONDS)
        assert not due(last_flush=0.0, now=probe_rollup.ROLLUP_FLUSH_INTERVAL_SECONDS - 1)
        assert due(last_flush=0.0, now=probe_rollup.ROLLUP_FLUSH_INTERVAL_SECONDS)
        assert due(last_flush=100.0, now=100.0 + probe_rollup.ROLLUP_FLUSH_INTERVAL_SECONDS)
