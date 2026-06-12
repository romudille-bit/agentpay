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


# ── POST /discovery/arbitrum/verify (route wiring + replay consume) ──────────

import pytest


def _verify_body(tx="0x" + "a" * 64):
    return {
        "tx_hash": tx,
        "payment_id": "0x" + "1" * 64,
        "payer": "0x" + "b" * 40,
        "developer": "0x" + "d" * 40,
        "amount_usdc": "0.01",
        "chain": "arbitrum-sepolia",
    }


class TestRadarVerifyRoute:

    @pytest.fixture(autouse=True)
    def configured(self, monkeypatch):
        import gateway.routes.discovery as disco
        monkeypatch.setattr(disco.settings, "RADAR_CONTRACT_ARBITRUM_SEPOLIA", "0x" + "5" * 40)
        disco._consumed_radar_txs.clear()

        async def fake_verify(**kwargs):
            return {"success": True, "reason": "ok", "tx_hash": kwargs["tx_hash"],
                    "dev_amount": 10000, "fee": 0}
        import gateway.radar_settle as rs
        monkeypatch.setattr(rs, "verify_radar_settlement", fake_verify)

    def test_verify_success_and_consume(self, client):
        r = client.post("/discovery/arbitrum/verify", json=_verify_body())
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["chain"] == "arbitrum-sepolia"

    def test_same_tx_rejected_second_time(self, client):
        b = _verify_body(tx="0x" + "e" * 64)
        r1 = client.post("/discovery/arbitrum/verify", json=b)
        r2 = client.post("/discovery/arbitrum/verify", json=b)
        assert r1.json()["success"] is True
        assert r2.json()["success"] is False
        assert "replay" in r2.json()["reason"]

    def test_unconfigured_chain_503(self, client):
        b = _verify_body()
        b["chain"] = "robinhood"   # contract/RPC unset in tests
        r = client.post("/discovery/arbitrum/verify", json=b)
        assert r.status_code == 503

    def test_unknown_chain_422(self, client):
        b = _verify_body()
        b["chain"] = "dogechain"
        r = client.post("/discovery/arbitrum/verify", json=b)
        assert r.status_code == 422
