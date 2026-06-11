"""
tests/test_radar_settle.py — pure-function tests for the RadarSplit settlement verifier.

Crafts tx receipts with a `Settled` event and runs them through parse_settled_log.
No network. Confirms the verifier checks ALL fields (contract, paymentId, payer,
developer, amount, feeRecipient) — not just paymentId.
"""

from gateway import radar_settle as rs

CONTRACT = "0x00000000000000000000000000000000DeaDBeeF"
PID = "0x" + "ab" * 32
PAYER = "0x1111111111111111111111111111111111111111"
DEV = "0x2222222222222222222222222222222222222222"
GATEWAY = "0x3333333333333333333333333333333333333333"


def _word_addr(a: str) -> str:
    return a.lower().removeprefix("0x").zfill(64)


def _word_uint(n: int) -> str:
    return hex(n)[2:].zfill(64)


def _settled_log(contract, pid, payer, dev, dev_amount, fee, fee_recipient):
    return {
        "address": contract,
        "topics": [
            rs.SETTLED_TOPIC0,
            "0x" + pid.removeprefix("0x").zfill(64),
            "0x" + _word_addr(payer),
            "0x" + _word_addr(dev),
        ],
        "data": "0x" + _word_uint(dev_amount) + _word_uint(fee) + _word_addr(fee_recipient),
    }


def _receipt(logs, status="0x1"):
    return {"status": status, "logs": logs}


def test_valid_zero_fee_settlement():
    r = _receipt([_settled_log(CONTRACT, PID, PAYER, DEV, 2000, 0, GATEWAY)])
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 2000)
    assert out["success"] and out["dev_amount"] == 2000 and out["fee"] == 0


def test_valid_with_fee_and_recipient_check():
    r = _receipt([_settled_log(CONTRACT, PID, PAYER, DEV, 850, 150, GATEWAY)])
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 1000, fee_recipient=GATEWAY)
    assert out["success"] and out["dev_amount"] == 850 and out["fee"] == 150


def test_wrong_contract_rejected():
    r = _receipt([_settled_log("0x" + "99" * 20, PID, PAYER, DEV, 2000, 0, GATEWAY)])
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 2000)
    assert not out["success"] and out["reason"] == "no_matching_settled_event"


def test_wrong_payer_rejected():
    r = _receipt([_settled_log(CONTRACT, PID, "0x" + "44" * 20, DEV, 2000, 0, GATEWAY)])
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 2000)
    assert not out["success"]


def test_wrong_developer_rejected():
    r = _receipt([_settled_log(CONTRACT, PID, PAYER, "0x" + "55" * 20, 2000, 0, GATEWAY)])
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 2000)
    assert not out["success"]


def test_insufficient_amount_rejected():
    r = _receipt([_settled_log(CONTRACT, PID, PAYER, DEV, 500, 0, GATEWAY)])
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 2000)
    assert not out["success"] and "insufficient" in out["reason"]


def test_fee_recipient_mismatch_rejected():
    r = _receipt([_settled_log(CONTRACT, PID, PAYER, DEV, 850, 150, "0x" + "66" * 20)])
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 1000, fee_recipient=GATEWAY)
    assert not out["success"] and out["reason"] == "fee_recipient_mismatch"


def test_reverted_tx_rejected():
    r = _receipt([_settled_log(CONTRACT, PID, PAYER, DEV, 2000, 0, GATEWAY)], status="0x0")
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 2000)
    assert not out["success"] and out["reason"] == "tx_reverted"


def test_no_receipt():
    out = rs.parse_settled_log(None, CONTRACT, PID, PAYER, DEV, 2000)
    assert not out["success"] and out["reason"] == "no_receipt"


def test_total_counts_dev_plus_fee():
    # dev 850 + fee 150 = 1000 meets a 1000 requirement even though dev alone < 1000
    r = _receipt([_settled_log(CONTRACT, PID, PAYER, DEV, 850, 150, GATEWAY)])
    out = rs.parse_settled_log(r, CONTRACT, PID, PAYER, DEV, 1000)
    assert out["success"]
