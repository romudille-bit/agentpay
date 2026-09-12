"""
test_budget_policy_stacks.py — spending rules beyond the cap, on the Stacks rail.

A policy-configured Session refuses, before anything is signed: a 402 whose
payee is not on allowed_recipients; a call over max_per_call; a call above
approve_above that no approver confirmed. Refusals show up in the receipt as
anomalies. The sBTC balance read mirrors the Horizon contract (0 when empty,
RuntimeError when the API is down) so budget_policy never clamps to $0 on an
infra blip.

Wallets are real AgentWallets with a real Stacks keypair; the gateway and
Hiro are respx mocks. A signed transaction reaching the gateway is the
failure condition for every "refused" test — the POST mock counts calls.
"""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from stellar_sdk import Keypair

from agentpay import budget_policy
from agentpay._wallet import (
    AgentWallet, ApprovalRequired, BudgetExceeded, PolicyRejected, Session,
)

GATEWAY = "https://gateway-fake.example"
TOOL = "verified_route"
TOOL_URL = f"{GATEWAY}/tools/{TOOL}/call"
HIRO_ACCOUNTS = r"https://api\.testnet\.hiro\.so/v2/accounts/.*"
HIRO_BALANCES = r"https://api\.testnet\.hiro\.so/extended/v1/address/.*/balances"

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "stacks_tx_fixtures.json").read_text()
)
STACKS_KEY = FIXTURES["keys"][0]["private_key"]
PAYEE = FIXTURES["keys"][1]["address_testnet"]          # the gateway's payee
OTHER = FIXTURES["keys"][2]["address_testnet"] if len(FIXTURES["keys"]) > 2 \
    else "ST1PQHQKV0RJXZFY1DGX8MNSNYVE3VGZJSRTPGZGM"


def _402(amount_usdc="0.01", pay_to=PAYEE):
    from agentpay._stacks_tx import sats_from_usd
    return {
        "payment_id": "pay_policy_0001",
        "amount_usdc": amount_usdc,
        "pay_to": "GFAKEPAYTOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "payment_options": {"stacks": {
            "amount_sats": sats_from_usd(Decimal(amount_usdc), Decimal("100000")),
            "amount_usdc": amount_usdc,
            "btc_usd_rate": "100000",
            "pay_to": pay_to,
            "network": "stacks:2147483648",
            "fee_microstx": 500,
            "scheme": "exact",
        }},
    }


def _ok(amount="0.01"):
    return {"tool": TOOL, "result": {"ok": True},
            "payment": {"amount_usdc": amount, "network": "stacks", "tx_hash": "ab" * 32}}


def _wallet():
    return AgentWallet(secret_key=Keypair.random().secret, network="testnet",
                       stacks_key=STACKS_KEY)


def _mock_gateway(price="0.01", pay_to=PAYEE):
    respx.get(f"{GATEWAY}/tools/{TOOL}").mock(return_value=httpx.Response(200, json={
        "name": TOOL, "price_usdc": price, "category": "data"}))
    respx.get(f"{GATEWAY}/tools").mock(return_value=httpx.Response(200, json={"tools": []}))
    respx.get(url__regex=HIRO_ACCOUNTS).mock(
        return_value=httpx.Response(200, json={"nonce": 3, "balance": "0x0"}))
    return respx.post(TOOL_URL).mock(side_effect=[
        httpx.Response(402, json=_402(price, pay_to)),
        httpx.Response(200, json=_ok(price)),
    ])


class TestRecipientAllowlist:

    def test_allowlisted_payee_settles(self):
        with respx.mock:
            route = _mock_gateway()
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                        allowed_recipients=[PAYEE])
            r = s.call(TOOL, {"need": "x"})
        assert r.data == {"ok": True}
        assert route.call_count == 2                      # 402 then the signed retry
        assert s.spending_summary()["anomalies"] == []

    def test_unlisted_payee_refused_before_signing(self):
        with respx.mock:
            route = _mock_gateway(pay_to=OTHER)
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                        allowed_recipients=[PAYEE])
            with pytest.raises(PolicyRejected) as ei:
                s.call(TOOL, {"need": "x"})
        assert ei.value.rule == "allowed_recipients"
        assert ei.value.chain == "stacks" and ei.value.pay_to == OTHER
        assert isinstance(ei.value, BudgetExceeded)      # existing handlers still catch it
        assert route.call_count == 1                      # only the unpaid probe; nothing signed
        assert s.spent_usd() == 0
        flags = s.spending_summary()["anomalies"]
        assert flags == [{"flag": "policy_rejected", "rule": "allowed_recipients",
                          "count": 1, "detail": "1 call(s) refused by allowed_recipients"}]

    def test_allowlist_is_exact_on_the_c32_string(self):
        # A lowercase or truncated copy of the payee is a different recipient.
        with respx.mock:
            _mock_gateway(pay_to=PAYEE)
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                        allowed_recipients=[PAYEE.lower()])
            with pytest.raises(PolicyRejected):
                s.call(TOOL, {"need": "x"})


class TestPerCallMax:

    def test_within_per_call_max_settles(self):
        with respx.mock:
            _mock_gateway(price="0.01")
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                        max_per_call="0.02")
            assert s.call(TOOL, {"need": "x"}).data == {"ok": True}

    def test_over_per_call_max_refused_on_the_quote(self):
        """The registry price already exceeds the rule: no HTTP at all."""
        with respx.mock:
            route = _mock_gateway(price="0.03")
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                        max_per_call="0.02")
            with pytest.raises(PolicyRejected) as ei:
                s.call(TOOL, {"need": "x"})
        assert ei.value.rule == "max_per_call"
        assert route.call_count == 0
        assert s.remaining_usd() == Decimal("0.05")       # no hold left behind

    def test_over_per_call_max_refused_on_the_402(self):
        """Quoted under the rule, demanded over it: the 402 amount is what
        would be signed, so that is what the rule binds."""
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/{TOOL}").mock(return_value=httpx.Response(200, json={
                "name": TOOL, "price_usdc": "0.01", "category": "data"}))
            respx.get(f"{GATEWAY}/tools").mock(return_value=httpx.Response(200, json={"tools": []}))
            respx.get(url__regex=HIRO_ACCOUNTS).mock(
                return_value=httpx.Response(200, json={"nonce": 3, "balance": "0x0"}))
            route = respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_402("0.03")),
            ])
            s = Session(_wallet(), GATEWAY, max_spend="0.10", prefer_chain="stacks",
                        max_per_call="0.02")
            with pytest.raises(BudgetExceeded):
                s.call(TOOL, {"need": "x"})
        assert route.call_count == 1
        assert s.spent_usd() == 0


class TestApprovalGate:

    def test_above_threshold_without_approver_requires_approval(self):
        with respx.mock:
            route = _mock_gateway(price="0.02")
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                        approve_above="0.01")
            with pytest.raises(ApprovalRequired) as ei:
                s.call(TOOL, {"need": "x"})
        assert ei.value.rule == "approve_above" and ei.value.amount == "0.02"
        assert route.call_count == 1 and s.spent_usd() == 0
        assert s.spending_summary()["anomalies"][0]["flag"] == "approval_required"

    def test_approver_sees_the_402_and_can_allow(self):
        seen = []
        def approver(req):
            seen.append(req)
            return req["chain"] == "stacks" and req["pay_to"] == PAYEE
        with respx.mock:
            route = _mock_gateway(price="0.02")
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                        approve_above="0.01", approver=approver)
            assert s.call(TOOL, {"need": "x"}).data == {"ok": True}
        assert route.call_count == 2
        assert seen == [{"tool": TOOL, "chain": "stacks", "pay_to": PAYEE,
                         "amount_usd": "0.02", "threshold": "0.01"}]
        assert s.spending_summary()["anomalies"] == []

    def test_approver_denial_or_crash_refuses(self):
        for approver in (lambda req: False, lambda req: 1 / 0):
            with respx.mock:
                route = _mock_gateway(price="0.02")
                s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                            approve_above="0.01", approver=approver)
                with pytest.raises(ApprovalRequired):
                    s.call(TOOL, {"need": "x"})
            assert route.call_count == 1

    def test_below_threshold_never_asks(self):
        with respx.mock:
            _mock_gateway(price="0.01")
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks",
                        approve_above="0.01", approver=lambda req: 1 / 0)
            assert s.call(TOOL, {"need": "x"}).data == {"ok": True}


class TestReceiptAnomalies:

    def test_repeated_identical_paid_call_is_flagged(self):
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/{TOOL}").mock(return_value=httpx.Response(200, json={
                "name": TOOL, "price_usdc": "0.01", "category": "data"}))
            respx.get(f"{GATEWAY}/tools").mock(return_value=httpx.Response(200, json={"tools": []}))
            respx.get(url__regex=HIRO_ACCOUNTS).mock(
                return_value=httpx.Response(200, json={"nonce": 3, "balance": "0x0"}))
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_402("0.01")), httpx.Response(200, json=_ok()),
            ] * 3)
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks")
            for _ in range(3):
                s.call(TOOL, {"need": "x"})
        flags = {f["flag"]: f for f in s.spending_summary()["anomalies"]}
        assert flags["repeated_call"]["count"] == 3 and flags["repeated_call"]["tool"] == TOOL
        assert "large_single_call" not in flags

    def test_large_single_call_is_flagged(self):
        with respx.mock:
            _mock_gateway(price="0.03")
            s = Session(_wallet(), GATEWAY, max_spend="0.05", prefer_chain="stacks")
            s.call(TOOL, {"need": "x"})
        flags = [f["flag"] for f in s.spending_summary()["anomalies"]]
        assert flags == ["large_single_call"]

    def test_quiet_session_has_no_anomalies(self):
        s = Session(_wallet(), GATEWAY, max_spend="0.05")
        assert s.anomalies() == []


class TestSbtcBalance:

    def _balances(self, sats):
        from agentpay._stacks_tx import SBTC_CONTRACT_TESTNET
        return {"stx": {"balance": "1000000"},
                "fungible_tokens": {f"{SBTC_CONTRACT_TESTNET}::sbtc-token": {"balance": str(sats)}}}

    def test_balance_in_sats_and_usd(self):
        w = _wallet()
        with respx.mock:
            respx.get(url__regex=HIRO_BALANCES).mock(
                return_value=httpx.Response(200, json=self._balances(2000)))
            assert w.get_sbtc_balance() == "2000"
            assert w.get_sbtc_balance_usd(100_000) == "2"     # 2,000 sats at $100k

    def test_no_sbtc_is_zero(self):
        w = _wallet()
        with respx.mock:
            respx.get(url__regex=HIRO_BALANCES).mock(
                return_value=httpx.Response(200, json={"stx": {"balance": "0"}, "fungible_tokens": {}}))
            assert w.get_sbtc_balance() == "0"

    def test_hiro_down_raises_never_zero(self):
        w = _wallet()
        with respx.mock:
            respx.get(url__regex=HIRO_BALANCES).mock(side_effect=httpx.ConnectError("down"))
            with pytest.raises(RuntimeError, match="balance check failed"):
                w.get_sbtc_balance()
        with respx.mock:
            respx.get(url__regex=HIRO_BALANCES).mock(return_value=httpx.Response(502))
            with pytest.raises(RuntimeError, match="balance check failed"):
                w.get_sbtc_balance()

    def test_budget_policy_clamps_to_sbtc_value(self):
        w = _wallet()
        with respx.mock:
            respx.get(url__regex=HIRO_BALANCES).mock(
                return_value=httpx.Response(200, json=self._balances(1500)))
            usd = w.get_sbtc_balance_usd(100_000)          # $1.50
        d = budget_policy(balance_usd=usd, pct_of_balance=0.10, max_cap="0.25")
        assert d.max_spend == "0.15" and d.source == "policy"
        d2 = budget_policy(explicit="5.00", balance_usd=usd)
        assert d2.max_spend == "1.5" and d2.capped_by_balance


class TestRulesAreRailAgnostic:
    """The same rules bind Base and Stellar payments — the check runs on the
    chosen option's recipient and amount, whatever the rail."""

    def test_pre_pay_check_on_base_and_stellar(self):
        s = Session(_wallet(), GATEWAY, max_spend="0.05",
                    allowed_recipients=["0x" + "e" * 40, "G" + "A" * 55],
                    max_per_call="0.02")
        s._pre_pay_check(tool="t", chain="base", pay_to="0x" + "e" * 40, amount_usd="0.01")
        s._pre_pay_check(tool="t", chain="stellar", pay_to="G" + "A" * 55, amount_usd="0.02")
        with pytest.raises(PolicyRejected) as ei:
            s._pre_pay_check(tool="t", chain="base", pay_to="0x" + "f" * 40, amount_usd="0.01")
        assert ei.value.chain == "base"
        with pytest.raises(PolicyRejected) as ei:
            s._pre_pay_check(tool="t", chain="stellar", pay_to="G" + "A" * 55, amount_usd="0.03")
        assert ei.value.rule == "max_per_call"
        # $0 never gates, whatever the recipient.
        s._pre_pay_check(tool="t", chain="base", pay_to="0x" + "f" * 40, amount_usd="0")
