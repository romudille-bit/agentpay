"""
test_stacks_cap_binds_signature.py

The cap is enforced in USD, but amount_sats is what leaves the wallet.
assert_sats_within_cap must refuse to sign a sats amount the cap doesn't bound.
"""
import pytest
from decimal import Decimal

from agentpay._stacks_tx import assert_sats_within_cap, sats_from_usd

RATE = "118000"  # ~ BTC/USD


def _consistent(usd, rate=RATE):
    return sats_from_usd(Decimal(usd), Decimal(rate))


def test_consistent_quote_passes():
    assert_sats_within_cap(_consistent("0.01"), "0.01", RATE)  # no raise


def test_rejects_inflated_sats():
    # $0.01 claimed, but 1 BTC of sats signed
    with pytest.raises(ValueError):
        assert_sats_within_cap(100_000_000, "0.01", RATE)


def test_rejects_subfloor_rate():
    # a rate below the $10k floor is refused even if internally consistent
    with pytest.raises(ValueError):
        assert_sats_within_cap(_consistent("0.01", "5000"), "0.01", "5000")


def test_floor_bound_holds_without_rate():
    # $0.01 at the $10k floor buys at most 100 sats
    assert_sats_within_cap(90, "0.01", None)
    with pytest.raises(ValueError):
        assert_sats_within_cap(101, "0.01", None)


def test_missing_usd_refused():
    with pytest.raises(ValueError):
        assert_sats_within_cap(10, None, RATE)


def test_env_floor_override(monkeypatch):
    monkeypatch.setenv("STACKS_MIN_BTC_USD", "50000")  # $0.01 -> at most 20 sats
    assert_sats_within_cap(20, "0.01", None)
    with pytest.raises(ValueError):
        assert_sats_within_cap(21, "0.01", None)
