"""
test_flagship_stacks_rail.py — the flagship analyst on the Stacks rail.

The daily cron can settle its gateway-paid calls in sBTC on Stacks instead
of USDC on Base. Pinned here: which rail a run picks, which address the
ledger is told paid, and how a Stacks payment that outlives the gateway's
settle window is redeemed rather than dropped.
"""

import types

import httpx
import pytest

from agentpay import PaymentFailed, RefundPending, SettlementUncertain
from agents.analyst.run import (
    _redeem_uncertain,
    payer_address,
    select_rail,
    session_kwargs,
)


class TestSelectRail:

    def test_no_key_is_base_whatever_the_env_says(self):
        assert select_rail(0) == "base"
        assert select_rail(1, "stacks", has_stacks_key=False) == "base"
        assert select_rail(1, "alternate", has_stacks_key=False) == "base"

    def test_key_defaults_to_stacks(self):
        assert select_rail(0, "", has_stacks_key=True) == "stacks"
        assert select_rail(7, "   ", has_stacks_key=True) == "stacks"

    def test_explicit_pins(self):
        assert select_rail(0, "base", has_stacks_key=True) == "base"
        assert select_rail(0, "STACKS", has_stacks_key=True) == "stacks"

    def test_alternate_splits_by_day(self):
        rails = [select_rail(d, "alternate", has_stacks_key=True) for d in range(6)]
        assert rails == ["base", "stacks", "base", "stacks", "base", "stacks"]

    def test_garbage_env_falls_back_to_key_rule(self):
        assert select_rail(0, "solana", has_stacks_key=True) == "stacks"
        assert select_rail(0, "solana", has_stacks_key=False) == "base"


class TestPayerAddress:

    def test_rail_picks_the_address(self):
        w = types.SimpleNamespace(
            base_address="0xe1601C10B8d4DbF71E0c592B779520380174bc3A",
            stacks_address="SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5",
        )
        assert payer_address(w, "stacks") == w.stacks_address
        assert payer_address(w, "base") == w.base_address

    def test_missing_stacks_address_is_empty_not_base(self):
        # A Stacks run must never be attributed to the Base wallet on /ledger.
        w = types.SimpleNamespace(base_address="0x" + "a" * 40, stacks_address=None)
        assert payer_address(w, "stacks") == ""


class TestSessionKwargs:
    """The Session is built from kwargs the installed SDK accepts. The cron
    installs the SDK from PyPI on its own pin, so the run must not die on an
    argument the running SDK is too old to know."""

    class _Current:
        def __init__(self, wallet, max_spend="0.10", gateway_url=None,
                     prefer_chain=None, max_per_call=None):
            pass

    class _Older:
        def __init__(self, wallet, max_spend="0.10", gateway_url=None,
                     prefer_chain=None, allowed_tools=None, max_per_tool=None):
            pass

    def test_current_sdk_gets_the_per_leg_ceiling(self):
        kw = session_kwargs(self._Current, "stacks", "0.02", lambda m: None)
        assert kw == {"prefer_chain": "stacks", "max_per_call": "0.02"}
        assert session_kwargs(self._Current, "base", "0.02", lambda m: None) == {"max_per_call": "0.02"}

    def test_older_sdk_runs_on_the_cap_alone_and_says_so(self):
        logs = []
        kw = session_kwargs(self._Older, "stacks", "0.02", logs.append)
        assert kw == {"prefer_chain": "stacks"}
        assert any("max_per_call" in m for m in logs)
        self._Older(wallet=None, **kw)

    @pytest.mark.parametrize("bad", ["", "2c", "$0.02", "0", "-1"])
    def test_unreadable_ceiling_stops_the_run_before_any_call(self, bad):
        logs = []
        assert session_kwargs(self._Current, "base", bad, logs.append) is None
        assert any("FATAL" in m for m in logs)


class _Redeemer:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kw = None

    def redeem(self, exc, **kw):
        self.kw = kw
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class TestRedeemUncertain:

    def _exc(self):
        return SettlementUncertain("deadline", tx_hash="ab" * 32, network="stacks")

    def test_confirmed_tx_returns_the_tool_result(self):
        s = _Redeemer({"result": {"verdict": "OK"}, "payment": {"status": "verified"}})
        out = _redeem_uncertain(s, self._exc(), lambda m: None)
        assert out == {"verdict": "OK"}
        assert s.kw["wait_s"] >= 60 and s.kw["poll_s"] > 0

    @pytest.mark.parametrize("exc", [
        SettlementUncertain("still unconfirmed", tx_hash="ab" * 32),
        PaymentFailed("aborted on-chain"),
        # Redeem re-presents the tx over HTTP, so a transport error is an
        # ordinary outcome. It runs inside `except SettlementUncertain`, where
        # the loop's other handlers no longer apply — anything escaping kills
        # the run after the money is spent.
        httpx.ReadTimeout("redeem timed out"),
        httpx.ConnectError("gateway unreachable"),
        RuntimeError("something unforeseen"),
    ])
    def test_unfinished_or_refused_redeem_is_none_not_a_crash(self, exc):
        logs = []
        out = _redeem_uncertain(_Redeemer(exc), self._exc(), logs.append)
        assert out is None
        assert any("redeem did not complete" in m for m in logs)


class TestPaidLoopOnStacks:
    """The paid loop as shipped: an uncertain settle is redeemed into a
    verdict; a redeem that does not finish is a skip, not a dead run."""

    def _loop(self, s, log):
        verdicts, skipped = {}, {}
        for sym in ("BTC",):
            try:
                r = s.call("pre_trade_check", {"symbol": sym})
                verdicts[sym] = r.data
            except SettlementUncertain as e:
                data = _redeem_uncertain(s, e, log)
                if data is not None:
                    verdicts[sym] = data
                else:
                    skipped[sym] = "settlement uncertain"
            except (PaymentFailed, RefundPending) as e:
                skipped[sym] = "payment failed"
            except Exception as e:
                skipped[sym] = f"error: {type(e).__name__}"
        return verdicts, skipped

    def test_source_redeems_before_treating_uncertain_as_failed(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "agents/analyst/run.py").read_text()
        # SettlementUncertain subclasses PaymentFailed: its handler has to
        # come first or the redeem branch is dead code.
        paid_sites = src.split('s.call("pre_trade_check"')[1:]
        assert paid_sites, "paid call site moved"
        site = paid_sites[0][:1200]
        assert site.index("except SettlementUncertain") < site.index(
            "except (PaymentFailed, RefundPending)")

    def test_uncertain_then_confirmed_yields_a_verdict(self):
        class S(_Redeemer):
            def call(self, *_a, **_k):
                raise SettlementUncertain("deadline", tx_hash="cd" * 32, network="stacks")
        s = S({"result": {"verdict": "CAUTION"}})
        verdicts, skipped = self._loop(s, lambda m: None)
        assert verdicts == {"BTC": {"verdict": "CAUTION"}} and skipped == {}

    def test_uncertain_that_never_confirms_is_a_skip(self):
        class S(_Redeemer):
            def call(self, *_a, **_k):
                raise SettlementUncertain("deadline", tx_hash="cd" * 32, network="stacks")
        s = S(SettlementUncertain("still pending", tx_hash="cd" * 32))
        verdicts, skipped = self._loop(s, lambda m: None)
        assert verdicts == {} and skipped == {"BTC": "settlement uncertain"}


class TestRunCapValidation:
    """The run cap gets the same treatment session_kwargs gave the per-leg
    ceiling: it is passed straight to Session(max_spend=), which parses
    strictly, so a typo in the Railway variable would raise from inside the SDK
    at construction — a dead run with nothing on the ledger."""

    @pytest.mark.parametrize("bad", ["O.25", "$0.25", "0", "-1", "abc"])
    def test_an_unusable_cap_stops_the_run_before_any_call(self, bad, monkeypatch):
        import agents.analyst.run as analyst_run

        logs = []
        monkeypatch.setattr(analyst_run, "log", logs.append)
        monkeypatch.setenv("FLAGSHIP_MAX_SPEND", bad)
        monkeypatch.setenv("FLAGSHIP_STELLAR_SECRET", "")
        monkeypatch.setenv("FLAGSHIP_BASE_KEY", "")

        assert analyst_run.main() == 1
        assert any("FLAGSHIP_MAX_SPEND" in m and "FATAL" in m for m in logs), logs
