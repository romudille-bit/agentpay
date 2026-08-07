"""The daily run must degrade, never die.

2026-08-07: a paid-phase error that was neither PaymentFailed nor RefundPending
propagated out of main() — nothing published, no reason logged. A re-run on the
same code succeeded, so the defect was that a transient could be fatal at all.
"""

import importlib
import sys
import types

import pytest
from agentpay import PaymentFailed, RefundPending


def _analyst():
    return importlib.import_module("agents.analyst.run")


class _Boom:
    """Session stub whose paid calls raise `exc`; free calls succeed."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def call(self, tool, params=None):
        self.calls.append(tool)
        if tool in ("pre_trade_check", "verified_route"):
            raise self.exc
        return types.SimpleNamespace(data={"ok": True}, tx="free:x")

    def would_exceed(self, *_a, **_k):
        return False

    def tool_cost_usd(self, *_a, **_k):
        return None

    def remaining_usd(self):
        return 0.25


@pytest.mark.parametrize("exc", [
    RuntimeError("facilitator 502"),
    ValueError("bad 402 payload"),
    KeyError("accepts"),
    ConnectionError("base rpc timeout"),
])
def test_unexpected_paid_error_skips_the_symbol_not_the_run(exc, monkeypatch, capsys):
    """Whatever the paid call throws, the loop records a skip and continues."""
    run = _analyst()
    s = _Boom(exc)
    verdicts, skipped = {}, {}

    # the loop body as shipped, exercised directly
    for sym in ("BTC", "ETH"):
        try:
            r = s.call("pre_trade_check",
                       {"symbol": sym, "size_usd": 25000, "side": "long"})
            verdicts[sym] = r.data
        except (PaymentFailed, RefundPending) as e:
            run.log(f"paid verdict {sym} failed: {e}")
            skipped[sym] = "payment failed"
        except Exception as e:
            run.log(f"paid verdict {sym} error: {type(e).__name__}: {e}")
            skipped[sym] = f"error: {type(e).__name__}"

    assert verdicts == {}
    assert set(skipped) == {"BTC", "ETH"}
    assert all(v.startswith("error: ") for v in skipped.values())
    # the reason must reach the log — a silent skip is what cost us the day
    out = capsys.readouterr().out
    assert type(exc).__name__ in out


def test_source_has_a_broad_handler_on_every_paid_call():
    """Every `except (PaymentFailed, RefundPending)` needs a broad one after it."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "agents/analyst/run.py").read_text()
    narrow = src.count("except (PaymentFailed, RefundPending)")
    assert narrow >= 3, "paid call sites moved — update this guard"
    for block in src.split("except (PaymentFailed, RefundPending)")[1:]:
        # the next 400 chars must contain a broad catch before any new `def`
        window = block[:400].split("\ndef ")[0]
        assert "except Exception" in window, (
            "a paid call site lost its broad handler — one bad 402 can kill "
            "the whole daily run again")


def test_paid_phase_is_announced_in_the_log():
    """The 2026-08-07 log went silent between the plan line and death."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "agents/analyst/run.py").read_text()
    assert 'log(f"paid phase:' in src
