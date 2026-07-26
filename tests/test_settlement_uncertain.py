"""
test_settlement_uncertain.py — AGE-26.

The Stacks rail is sign-don't-broadcast: the gateway broadcasts and settlement
confirms asynchronously (testnet blocks take minutes). A transmitted-but-not-
yet-confirmed payment must surface as a typed, catchable outcome that carries
the txid — NOT a bare Exception, and NOT confused with a hard PaymentFailed.
"""

import pytest

from agentpay._wallet import PaymentFailed, SettlementUncertain


def test_subclasses_payment_failed():
    # Existing `except PaymentFailed` handlers must still catch it.
    assert issubclass(SettlementUncertain, PaymentFailed)


def test_carries_tx_and_network():
    e = SettlementUncertain("pending", tx_hash="0xabc", network="stacks")
    assert e.tx_hash == "0xabc"
    assert e.network == "stacks"


def test_defaults_empty():
    e = SettlementUncertain("x")
    assert e.tx_hash == ""
    assert e.network == ""


def test_exported_from_package():
    import agentpay
    assert agentpay.SettlementUncertain is SettlementUncertain


def test_catchable_as_payment_failed_with_tx():
    try:
        raise SettlementUncertain("broadcast", tx_hash="0xdeadbeef", network="stacks")
    except PaymentFailed as caught:
        # A caller catching the base class still recovers the txid.
        assert getattr(caught, "tx_hash", None) == "0xdeadbeef"
    else:
        pytest.fail("SettlementUncertain was not caught as PaymentFailed")
