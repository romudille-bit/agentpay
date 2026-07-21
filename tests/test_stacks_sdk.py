"""
test_stacks_sdk.py — AGE-25: the SDK's Stacks payment path.

chain="stacks" is sign-don't-broadcast: the SDK signs a complete sBTC
transfer (agentpay._stacks_tx, fixture-validated in test_stacks_tx.py) and
hands it to the gateway in the lowercase `payment-signature` header; the
GATEWAY broadcasts. These tests cover the SDK-side semantics:

  - the third header dialect (lowercase payment-signature, CAIP-2 network)
  - cap binding BEFORE signing (checklist #1) and spend-at-transmit (#2)
  - no cross-chain fallback, explicit chain is a hard requirement (#3)
  - client-side sequential nonce serialization — two concurrent paid calls
    from one wallet must use consecutive nonces (one in-flight tx per wallet)
  - stale-nonce rejection → re-sign exactly once
  - free ($0) tools never touch the Stacks path even when preferred
  - wallet-level spend counter moves on settle (#9)

Mocks at the httpx layer with respx (gateway 402/200 + the Hiro nonce read).
Wallets are REAL AgentWallets with a real Stacks keypair, so every test signs
real SIP-005 bytes — only the network is fake.
"""

import base64
import json
import threading
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from stellar_sdk import Keypair

from agentpay._client import AgentPayClient
from agentpay._wallet import AgentWallet, BudgetExceeded, PaymentFailed

GATEWAY = "https://gateway-fake.example"
TOOL_URL = f"{GATEWAY}/tools/verified_route/call"
HIRO_ACCOUNTS = r"https://api\.testnet\.hiro\.so/v2/accounts/.*"

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "stacks_tx_fixtures.json").read_text()
)
STACKS_KEY = FIXTURES["keys"][0]["private_key"]
RECIPIENT = FIXTURES["keys"][1]["address_testnet"]


def _stacks_402(amount_usdc="0.001", amount_sats=1030, **extra):
    opt = {
        "amount_sats": amount_sats,
        "amount_usdc": amount_usdc,
        "pay_to": RECIPIENT,
        "network": "stacks:2147483648",
        "fee_microstx": 500,
        "scheme": "exact",
    }
    opt.update(extra)
    return {
        "payment_id": "pay_stacks_test_0001",
        "amount_usdc": amount_usdc,
        "pay_to": "GFAKEPAYTOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "payment_options": {"stacks": opt},
    }


def _ok_200():
    return {
        "tool": "verified_route",
        "result": {"ok": True},
        "payment": {"amount_usdc": "0.001", "network": "stacks"},
    }


def _make_wallet(**kw) -> AgentWallet:
    return AgentWallet(
        secret_key=Keypair.random().secret, network="testnet",
        stacks_key=STACKS_KEY, **kw,
    )


def _decode_ps_header(header_value: str):
    """payment-signature → (payload dict, signed tx bytes, nonce)."""
    payload = json.loads(base64.b64decode(header_value))
    tx = bytes.fromhex(payload["payload"]["signedTransaction"])
    # SIP-005 single-sig layout: nonce is bytes 27..35 of the tx.
    nonce = int.from_bytes(tx[27:35], "big")
    return payload, tx, nonce


def _mock_nonce(value=7):
    return respx.get(url__regex=HIRO_ACCOUNTS).mock(
        return_value=httpx.Response(200, json={"nonce": value, "balance": "0x0"})
    )


# ── Happy path ───────────────────────────────────────────────────────────────


class TestStacksHappyPath:

    def test_paid_call_settles_with_lowercase_dialect(self):
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(7)
            route = respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                httpx.Response(200, json=_ok_200()),
            ])
            result = client.call_tool(
                "verified_route", {"need": "x"},
                max_spend="0.0011", prefer_chain="stacks", chain_is_explicit=True,
            )

        assert result["result"]["ok"] is True
        retry_req = route.calls[-1].request
        # Third dialect: lowercase payment-signature, NOT X-Payment /
        # PAYMENT-SIGNATURE-with-Base-payload.
        assert "payment-signature" in retry_req.headers
        assert retry_req.headers["x-agent-address"] == wallet.stacks_address
        payload, tx, nonce = _decode_ps_header(retry_req.headers["payment-signature"])
        assert payload["network"] == "stacks:2147483648"
        assert payload["accepted"]["asset"] == "sbtc"
        assert payload["payment_id"] == "pay_stacks_test_0001"
        assert nonce == 7
        # [CHECKLIST #5] challenge binding travels in the tx memo.
        assert b"pay_stacks_test_0001" in tx
        # txid in the payload matches the recorded entry.
        assert payload["payload"]["txid"] == client.call_log[-1]["tx_hash"]

    def test_entry_settled_and_wallet_counter_moves(self):
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(0)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                httpx.Response(200, json=_ok_200()),
            ])
            client.call_tool(
                "verified_route", {}, max_spend="0.0011",
                prefer_chain="stacks", chain_is_explicit=True,
            )
        e = client.call_log[-1]
        assert e["success"] is True
        assert e["state"] == "settled"
        assert e["network"] == "stacks"
        assert e["amount_usdc"] == "0.001"
        # [CHECKLIST #9] sign-don't-broadcast settle moves the wallet counter.
        assert wallet.total_spent_usdc == "0.001"
        # Sequential nonces: the settled leg's successor is cached.
        assert wallet._stacks_next_nonce == 1


# ── Hard-requirement failures (no fallback, checklist #1) ────────────────────


class TestStacksHardRequirement:

    def test_cap_binds_before_signing(self):
        """[CHECKLIST #1]: a 402 whose stacks option exceeds the cap is
        refused BEFORE any signing — no nonce fetch, no transmit."""
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        resp_402 = _stacks_402()  # body amount within cap...
        resp_402["payment_options"]["stacks"]["amount_usdc"] = "0.5"  # ...option not
        with respx.mock:
            nonce_route = _mock_nonce()
            route = respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=resp_402),
            ])
            with pytest.raises(BudgetExceeded, match="refusing to sign"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )
            assert nonce_route.call_count == 0
            assert route.call_count == 1  # only the initial 402
        assert wallet.total_spent_usdc == "0"

    def test_unparseable_usd_amount_fails_closed(self):
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        resp_402 = _stacks_402()
        resp_402["payment_options"]["stacks"]["amount_usdc"] = "not-a-number"
        with respx.mock:
            _mock_nonce()
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=resp_402),
            ])
            with pytest.raises(PaymentFailed, match="unparseable"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )

    def test_no_stacks_wallet_raises_with_reason(self):
        wallet = AgentWallet(secret_key=Keypair.random().secret, network="testnet")
        assert wallet.stacks_address is None
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
            ])
            with pytest.raises(PaymentFailed, match="STACKS_AGENT_KEY"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )

    def test_no_stacks_option_raises(self):
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with respx.mock:
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json={
                    "payment_id": "p1", "amount_usdc": "0.001",
                    "pay_to": "GFAKE" + "A" * 51,
                }),
            ])
            with pytest.raises(PaymentFailed, match="did not offer a Stacks"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )

    def test_wrong_network_option_refused(self):
        """A mainnet-CAIP-2 option against a testnet wallet must not sign."""
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce()
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402(network="stacks:1")),
            ])
            with pytest.raises(PaymentFailed, match="refusing to sign"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )


# ── Free tools ───────────────────────────────────────────────────────────────


class TestFreeToolWithStacksPreference:

    def test_free_flow_unchanged(self):
        """$0 challenges never settle on-chain: no signing, no nonce fetch,
        the free:<id> proof flow runs exactly as on other chains."""
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        free_402 = {
            "payment_id": "free-uuid-777", "amount_usdc": "0.000",
            "pay_to": "GFAKE" + "A" * 51,
        }
        with respx.mock:
            nonce_route = _mock_nonce()
            route = respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=free_402),
                httpx.Response(200, json={"tool": "verified_route",
                                          "result": {"ok": True},
                                          "payment": {"amount_usdc": "0.000"}}),
            ])
            result = client.call_tool(
                "verified_route", {}, max_spend="0.0011",
                prefer_chain="stacks", chain_is_explicit=True,
            )
        assert result["result"]["ok"] is True
        assert nonce_route.call_count == 0
        retry_req = route.calls[-1].request
        assert b"free:free-uuid-777" in retry_req.headers["X-Payment"].encode()
        assert "payment-signature" not in retry_req.headers
        assert client.call_log[-1]["amount_usdc"] == "0.000"


# ── Sequential nonces ────────────────────────────────────────────────────────


class TestNonceSerialization:

    def test_concurrent_calls_use_consecutive_nonces(self):
        """Two concurrent paid calls from ONE wallet must serialize: the
        chain keeps reporting nonce 5 (mempool lag), so only the local
        successor prevents a reuse. Both legs must settle, with nonces 5,6."""
        wallet = _make_wallet()
        seen_nonces = []

        def responder(request):
            if "payment-signature" in request.headers:
                _, _, nonce = _decode_ps_header(request.headers["payment-signature"])
                seen_nonces.append(nonce)
                return httpx.Response(200, json=_ok_200())
            return httpx.Response(402, json=_stacks_402())

        errors = []

        def leg():
            try:
                client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )
            except Exception as e:  # pragma: no cover
                errors.append(e)

        with respx.mock:
            respx.get(url__regex=HIRO_ACCOUNTS).mock(
                return_value=httpx.Response(200, json={"nonce": 5})
            )
            respx.post(TOOL_URL).mock(side_effect=responder)
            threads = [threading.Thread(target=leg) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert not errors
        assert sorted(seen_nonces) == [5, 6]
        assert wallet._stacks_next_nonce == 7
        assert wallet.total_spent_usdc == "0.002"

    def test_stale_nonce_rejection_resigns_once(self):
        """Gateway reports the broadcast was REJECTED for a nonce conflict —
        the tx is in no mempool, so the SDK re-fetches and re-signs ONCE."""
        wallet = _make_wallet()
        wallet._stacks_next_nonce = 3   # stale local successor
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        nonce_values = iter([2, 9])     # first read stale, post-reset read fresh

        def nonce_responder(request):
            return httpx.Response(200, json={"nonce": next(nonce_values)})

        with respx.mock:
            respx.get(url__regex=HIRO_ACCOUNTS).mock(side_effect=nonce_responder)
            route = respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                httpx.Response(502, json={
                    "payment_status": "rejected",
                    "error_reason": "broadcast rejected: BadNonce (expected 9)",
                }),
                httpx.Response(200, json=_ok_200()),
            ])
            result = client.call_tool(
                "verified_route", {}, max_spend="0.0011",
                prefer_chain="stacks", chain_is_explicit=True,
            )

        assert result["result"]["ok"] is True
        # Two signed transmissions with different nonces (3 then 9).
        sigs = [c.request.headers["payment-signature"]
                for c in route.calls if "payment-signature" in c.request.headers]
        assert len(sigs) == 2
        assert _decode_ps_header(sigs[0])[2] == 3
        assert _decode_ps_header(sigs[1])[2] == 9
        # The rejected leg is zeroed; only the settled leg carries spend.
        states = [(e["state"], e["amount_usdc"]) for e in client.call_log]
        assert ("stale_nonce_resigned", "0") in states
        assert ("settled", "0.001") in states
        total = sum(Decimal(e["amount_usdc"]) for e in client.call_log)
        assert total == Decimal("0.001")
        assert wallet._stacks_next_nonce == 10

    def test_second_stale_nonce_rejection_fails(self):
        """Only ONE re-sign: a second rejection surfaces as PaymentFailed."""
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        rejection = httpx.Response(502, json={
            "payment_status": "rejected",
            "error_reason": "broadcast rejected: ConflictingNonceInMempool",
        })
        with respx.mock:
            _mock_nonce(4)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                rejection, rejection,
            ])
            with pytest.raises(PaymentFailed, match="rejected"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )
        # Nothing settled → every leg zeroed.
        assert sum(Decimal(e["amount_usdc"]) for e in client.call_log) == 0
        assert wallet.total_spent_usdc == "0"


# ── Post-transmission failure modes (checklist #2/#3) ────────────────────────


class TestSpendRecordedNoFallback:

    def test_transport_error_after_transmit_keeps_spend(self):
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(1)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                httpx.ConnectError("boom"),
            ])
            with pytest.raises(Exception, match="settlement uncertain"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )
        e = client.call_log[-1]
        assert e["state"] == "uncertain_settlement"
        assert e["amount_usdc"] == "0.001"     # [CHECKLIST #2] spend recorded
        assert wallet._stacks_next_nonce == 2  # nonce treated as consumed

    def test_5xx_after_transmit_keeps_spend(self):
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(1)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                httpx.Response(500, text="edge exploded"),
            ])
            with pytest.raises(Exception, match="after payment"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )
        e = client.call_log[-1]
        assert e["state"] == "uncertain_settlement"
        assert e["amount_usdc"] == "0.001"

    def test_non_nonce_rejection_zeroes_spend(self):
        """A definitive rejection (post-condition abort) is $0 risk."""
        wallet = _make_wallet()
        client = AgentPayClient(wallet=wallet, gateway_url=GATEWAY)
        with respx.mock:
            _mock_nonce(1)
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                httpx.Response(502, json={
                    "payment_status": "rejected",
                    "error_reason": "abort_by_post_condition",
                }),
            ])
            with pytest.raises(PaymentFailed, match="rejected"):
                client.call_tool(
                    "verified_route", {}, max_spend="0.0011",
                    prefer_chain="stacks", chain_is_explicit=True,
                )
        assert client.call_log[-1]["amount_usdc"] == "0"
        assert client.call_log[-1]["state"] == "rejected"


# ── Wallet key handling ──────────────────────────────────────────────────────


class TestStacksWalletKey:

    def test_bad_key_constant_reason_no_echo(self):
        secret = "totally-not-a-key-12345"
        w = AgentWallet(secret_key=Keypair.random().secret,
                        network="testnet", stacks_key=secret)
        assert w.stacks_address is None
        assert w._stacks_keypair is None
        assert w.stacks_disabled_reason == (
            "Stacks key rejected: not a valid Stacks private key "
            "(64 hex, or 66 hex ending in 01)"
        )
        assert secret not in (w.stacks_disabled_reason or "")

    def test_good_key_derives_network_address(self):
        w = _make_wallet()
        assert w.stacks_address == FIXTURES["keys"][0]["address_testnet"]
        w2 = AgentWallet(secret_key=Keypair.random().secret,
                         network="mainnet", stacks_key=STACKS_KEY)
        assert w2.stacks_address == FIXTURES["keys"][0]["address_mainnet"]

    def test_no_key_no_stacks(self):
        w = AgentWallet(secret_key=Keypair.random().secret, network="testnet")
        assert w.stacks_address is None
        assert w.stacks_disabled_reason is None


# ── Session integration ──────────────────────────────────────────────────────


class TestSessionChainStacks:

    def test_session_call_chain_stacks_settles_and_bills(self):
        from agentpay._wallet import Session
        wallet = _make_wallet()
        with respx.mock:
            _mock_nonce(0)
            respx.get(f"{GATEWAY}/tools/verified_route").mock(
                return_value=httpx.Response(200, json={
                    "name": "verified_route", "price_usdc": "0.001",
                    "category": "data",
                })
            )
            route = respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
                httpx.Response(200, json=_ok_200()),
            ])
            s = Session(wallet, GATEWAY, max_spend="0.001")
            result = s.call("verified_route", {"need": "x"}, chain="stacks")

        assert result["result"]["ok"] is True
        assert "payment-signature" in route.calls[-1].request.headers
        assert s.spent_usd() == Decimal("0.001")
        assert s.remaining_usd() == Decimal("0")
        assert s._call_log[-1]["network"] == "stacks"

    def test_session_prefer_chain_stacks_without_wallet_hard_fails(self):
        from agentpay._wallet import Session
        wallet = AgentWallet(secret_key=Keypair.random().secret, network="testnet")
        with respx.mock:
            respx.get(f"{GATEWAY}/tools/verified_route").mock(
                return_value=httpx.Response(200, json={
                    "name": "verified_route", "price_usdc": "0.001",
                    "category": "data",
                })
            )
            respx.get(f"{GATEWAY}/tools").mock(
                return_value=httpx.Response(200, json={"tools": []})
            )
            respx.post(TOOL_URL).mock(side_effect=[
                httpx.Response(402, json=_stacks_402()),
            ])
            s = Session(wallet, GATEWAY, max_spend="0.01",
                        prefer_chain="stacks")
            with pytest.raises(PaymentFailed, match="stacks"):
                s.call("verified_route", {})
        # Hard requirement: no Stellar/Base leg was attempted, spend is zero.
        assert s.spent_usd() == 0
