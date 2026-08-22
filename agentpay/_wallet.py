"""
wallet.py — Agent-side Stellar wallet + budget session management.

Two main classes:
  AgentWallet  — Stellar wallet that sends USDC payments
  Session      — Budget-aware session with fallback routing
"""

import base64
import json
import httpx
import logging
import secrets
import threading
from decimal import Decimal

from stellar_sdk import (
    Keypair, Server, Network, Asset,
    TransactionBuilder
)

# ── Bazaar discovery endpoint (read-only, no API key required) ─────────────────
BAZAAR_SEARCH_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"

logger = logging.getLogger(__name__)

HORIZON_TESTNET = "https://horizon-testnet.stellar.org"
HORIZON_MAINNET = "https://horizon.stellar.org"

# ── Stacks (sBTC) settlement (AGE-25) ────────────────────────────────────────
STACKS_API_TESTNET = "https://api.testnet.hiro.so"
STACKS_API_MAINNET = "https://api.hiro.so"
# Suggested STX network fee when neither the 402's stacks option nor the
# STACKS_FEE_MICROSTX env var provides one. sBTC contract calls land
# comfortably under this on testnet; the gateway's 402 can always override.
DEFAULT_STACKS_FEE_MICROSTX = 3000
USDC_ISSUER_TESTNET = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
USDC_ISSUER_MAINNET = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"


# ── Exceptions ────────────────────────────────────────────────────────────────

class BudgetExceeded(Exception):
    """Raised when a tool call would exceed the session budget."""
    pass


class ToolNotFound(Exception):
    """Raised when the requested tool name does not exist on the gateway.

    A typo'd or unknown tool is an input error, not a budget problem — it is
    never substituted with another tool and never raises BudgetExceeded
    (AGE-118). Check the name against GET {gateway_url}/tools.
    """
    pass


class PaymentFailed(Exception):
    """
    Raised when the on-chain payment itself fails (insufficient funds,
    wallet not initialized, network error, etc.).

    The message is a short, human-readable reason like
    'stellar:op_underfunded' or 'stellar:tx_insufficient_fee' — NOT a raw
    XDR dump. Catch this in routine code to gracefully SKIP on payment
    errors without flooding logs.
    """
    pass


class UnsupportedChainPayment(PaymentFailed):
    """
    Raised when a 402's ONLY payment options are on chains this wallet cannot
    settle — e.g. a Base/Stellar wallet meeting an Avalanche-only
    (eip155:43114) or Arbitrum-only (eip155:42161) seller.

    This is NOT a settlement failure: no signature is produced and no value can
    move, so the seller must never be scored as a delivery failure. It
    subclasses PaymentFailed so existing ``except PaymentFailed`` handlers keep
    catching it, but carries the offered CAIP-2 networks so a caller (the
    Active Prober) can record the unserved-chain demand — a discovery signal,
    not a fault. (AGE-80)

    Attributes:
        offered_networks: CAIP-2 networks the 402 advertised that we can't
                          settle (authoritative — read from the live 402, not
                          from stale discovery metadata).
        settleable:       the chains this wallet CAN pay on.
    """
    def __init__(self, message: str, offered_networks=None, settleable=None):
        super().__init__(message)
        self.offered_networks = list(offered_networks or [])
        self.settleable = list(settleable or [])


class SettlementUncertain(PaymentFailed):
    """
    Raised when a signed payment WAS transmitted but the gateway could not
    confirm settlement within the request window. The transaction may be — and
    on the Stacks rail usually is — live on-chain, confirming asynchronously
    (Stacks testnet blocks take minutes; the gateway can't hold an open HTTP
    connection that long). Distinct from PaymentFailed (nothing settled) and
    RefundPending (settled, then the tool failed).

    The spend is recorded; DO NOT retry (a retry would double-pay) — verify
    ``tx_hash`` on-chain instead. Subclasses PaymentFailed so existing
    ``except PaymentFailed`` handlers still catch it.

    Attributes:
        tx_hash:  the transmitted transaction id, when known.
        network:  the settlement network ("stacks" / "base").
    """
    def __init__(self, message: str, *, tx_hash: str = "", network: str = ""):
        super().__init__(message)
        self.tx_hash = tx_hash
        self.network = network


class PrePaymentError(Exception):
    """
    Raised when a tool call fails BEFORE any funds move and BEFORE any
    signed payment authorization leaves the process — e.g. the initial
    request errored, the 402 couldn't be parsed, or the gateway returned
    an unexpected status on the un-paid probe.

    This is the ONLY failure class Session.call() will fall back on:
    anything else is treated as potentially-paid (fail closed) so a
    fallback can never turn into a second payment (AGE-55/AGE-56).
    """
    pass


class RefundPending(Exception):
    """
    Raised when the gateway accepted the payment on-chain but the tool
    execution itself failed. The gateway has marked the row for refund;
    the agent's USDC is on its way back (or already arrived).

    Surfaces the gateway's refund contract — the 502 response body carries `payment_status`,
    `refund_eta_seconds`, and `payment_id`, and this exception type
    lets callers branch on the failure mode without parsing JSON:

        try:
            result = session.call("token_price", {"symbol": "ETH"})
            use(result["result"])
        except RefundPending as e:
            log_warn(
                f"refund queued for {e.payment_id}, "
                f"tx will appear within ~{e.refund_eta_seconds}s"
            )
        except PaymentFailed:
            # On-chain payment failed (wallet empty, no trustline, etc.)
            skip()

    Attributes:
        payment_id: UUID echoed back by the gateway; cross-references
                    payment_logs row for manual reconciliation.
        refund_eta_seconds: gateway's estimate for when the refund tx
                    will appear on-chain. None when the gateway's
                    REFUND_ENABLED flag is False (dark-launch mode);
                    in that case the agent SHOULD treat it as
                    "lost until manually reconciled" and may want to
                    escalate.
        error_reason: short string describing what went wrong upstream,
                    starts with 'tool_exec_failed:' for the common case.
        payment_status: raw value from the gateway — either
                    'refund_pending' (worker will retry) or
                    'refund_disabled' (worker is off, manual handling
                    needed). Callers can branch on this if they want
                    sub-states without separate exception classes.
    """
    def __init__(
        self,
        message: str = "",
        *,
        payment_id: str = "",
        refund_eta_seconds = None,
        error_reason: str = "",
        payment_status: str = "",
    ):
        super().__init__(message or error_reason or "refund pending")
        self.payment_id = payment_id
        self.refund_eta_seconds = refund_eta_seconds
        self.error_reason = error_reason
        self.payment_status = payment_status


def _is_timeout_error(exc) -> bool:
    """AGE-68: does this exception look like a submit timeout / transport loss
    (as opposed to a clean protocol rejection like op_underfunded)? On these
    the tx may actually have been accepted, so the caller should poll for the
    precomputed hash before declaring failure."""
    # A stellar-sdk error carrying result_codes is a definitive on-chain
    # rejection — NOT a timeout — so never poll on those.
    extras = getattr(exc, "extras", None)
    if isinstance(extras, dict) and extras.get("result_codes"):
        return False
    name = exc.__class__.__name__.lower()
    text = f"{name} {str(exc)[:200]}".lower()
    needles = ("timeout", "timed out", "connect", "read", "temporarily",
               "connection", "504", "502", "503", "network")
    return any(n in text for n in needles)


def _extract_stellar_reason(exc) -> str:
    """
    Pull a short, clean reason string out of a stellar-sdk exception.

    Stellar errors carry the real cause in `extras.result_codes` — the
    str() of the exception itself can be a massive XDR dump that is
    useless in logs. This returns 'stellar:op_underfunded' or similar.
    """
    try:
        extras = getattr(exc, "extras", None) or {}
        if isinstance(extras, dict):
            codes = extras.get("result_codes") or {}
            if isinstance(codes, dict):
                ops = codes.get("operations")
                if isinstance(ops, list) and ops:
                    return f"stellar:{ops[0]}"
                tx = codes.get("transaction")
                if tx:
                    return f"stellar:{tx}"
        title = getattr(exc, "title", None) or getattr(exc, "message", None)
        if title:
            return f"stellar:{str(title)[:80]}"
    except Exception:
        pass
    first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return first[:200]


# ── Stellar Wallet ────────────────────────────────────────────────────────────

class AgentWallet:
    """
    Multi-network wallet for an AI agent.
    Supports Stellar (primary) and Base EVM (optional) USDC payments.

    Args:
        secret_key:   Stellar secret key (S...). Optional — omit for a
                      Stacks/Base-only wallet: an ephemeral Stellar identity
                      is generated and the Stellar pay path is disabled.
        network:      "mainnet" or "testnet" (applies to Stellar).
        base_key:     Optional Base/EVM private key (0x...) for paying
                      x402 tools that only accept Base USDC.
                      Read from env var BASE_AGENT_KEY if not passed.
        stacks_key:   Optional Stacks private key (64 hex, or 66 hex ending
                      in 01) for sBTC settlement over the Stacks x402 rail.
                      Read from env var STACKS_AGENT_KEY if not passed.

    Example:
        wallet = AgentWallet(
            secret_key=os.environ["STELLAR_SECRET"],
            network="mainnet",
            base_key=os.environ.get("BASE_AGENT_KEY"),
        )
    """

    # Base mainnet config
    BASE_RPC_URL   = "https://mainnet.base.org"
    BASE_CHAIN_ID  = 8453
    BASE_USDC      = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    # ERC20 transfer(address,uint256) selector
    _ERC20_TRANSFER_SIG = bytes.fromhex("a9059cbb")

    def __init__(self, secret_key: str = None, network: str = "testnet", *,
                 base_key: str = None, stacks_key: str = None):
        import os
        # secret_key is optional: a Stacks/Base-only payer can omit it. An
        # ephemeral Stellar keypair is generated so the wallet keeps a working
        # in-process identity (public_key, request signing), and the Stellar
        # pay path is guarded with a clear error instead of failing at Horizon
        # on an unfunded random account. Note the ephemeral identity differs
        # per process.
        self.stellar_ephemeral = secret_key is None
        if secret_key is None:
            self.keypair = Keypair.random()
        else:
            # AGE-74: wrap key parsing so a malformed secret raises a CONSTANT
            # message — a raw stellar_sdk error can echo fragments of the key
            # into logs/tracebacks.
            try:
                self.keypair = Keypair.from_secret(secret_key)
            except Exception:
                raise ValueError("invalid Stellar secret key (expected S...)") from None
        self.network = network
        self.server = Server(HORIZON_TESTNET if network == "testnet" else HORIZON_MAINNET)
        self.network_passphrase = (
            Network.TESTNET_NETWORK_PASSPHRASE
            if network == "testnet"
            else Network.PUBLIC_NETWORK_PASSPHRASE
        )
        self.usdc = Asset(
            "USDC",
            USDC_ISSUER_TESTNET if network == "testnet" else USDC_ISSUER_MAINNET
        )
        self._total_spent = Decimal("0")

        # ── Base/EVM wallet (optional) ────────────────────────────────────────
        # base_disabled_reason records WHY Base is unavailable so payment
        # errors can say so instead of silently degrading to Stellar.
        self.base_disabled_reason: str | None = None
        _base_key = base_key or os.environ.get("BASE_AGENT_KEY")
        if _base_key:
            try:
                from eth_account import Account as _Account
                self._evm_account = _Account.from_key(_base_key)
                self.base_address = self._evm_account.address
                logger.info(f"Base wallet loaded: {self.base_address[:10]}...")
            except ImportError:
                self.base_disabled_reason = (
                    "eth_account not installed — run: pip install \"agentpay-x402[base]\" "
                    "(if you have a venv, make sure it's activated)"
                )
                logger.warning(f"Base wallet init failed: {self.base_disabled_reason}")
                self._evm_account = None
                self.base_address = None
            except Exception:
                # AGE-74: CONSTANT message — never echo the exception text,
                # which can contain fragments of the private key.
                self.base_disabled_reason = (
                    "Base key rejected: not a valid EVM private key (0x + 64 hex)"
                )
                logger.warning("Base wallet init failed: invalid Base key — Base payments disabled")
                self._evm_account = None
                self.base_address = None
        else:
            self._evm_account = None
            self.base_address = None

        # ── Stacks/sBTC wallet (optional, AGE-25) ─────────────────────────────
        # sign-don't-broadcast: the SDK signs a complete sBTC transfer and
        # hands it to the gateway, which broadcasts (gateway/stacks.py).
        # stacks_disabled_reason records WHY Stacks is unavailable so payment
        # errors can say so instead of failing bare.
        self.stacks_disabled_reason: str | None = None
        self._stacks_keypair = None
        self.stacks_address: str | None = None
        # Serializes the whole sign→transmit→response leg: Stacks nonces are
        # sequential, so there is ONE in-flight signed tx per wallet
        # (docs/stacks-adapter.md). _stacks_next_nonce tracks our local
        # successor so back-to-back legs don't reuse a nonce the chain read
        # hasn't caught up to yet.
        self._stacks_lock = threading.Lock()
        self._stacks_next_nonce: int | None = None
        _stacks_key = stacks_key or os.environ.get("STACKS_AGENT_KEY")
        if _stacks_key:
            try:
                from agentpay._stacks_tx import StacksKeypair
                self._stacks_keypair = StacksKeypair.from_secret(_stacks_key)
                self.stacks_address = self._stacks_keypair.address(
                    "mainnet" if network == "mainnet" else "testnet"
                )
                logger.info(f"Stacks wallet loaded: {self.stacks_address[:8]}...")
            except ImportError:
                self.stacks_disabled_reason = (
                    "eth-keys not installed — run: pip install \"agentpay-x402[base]\" "
                    "(if you have a venv, make sure it's activated)"
                )
                logger.warning(f"Stacks wallet init failed: {self.stacks_disabled_reason}")
            except Exception:
                # [CHECKLIST #8]: CONSTANT message — never echo the exception
                # text, which can contain fragments of the private key.
                self.stacks_disabled_reason = (
                    "Stacks key rejected: not a valid Stacks private key "
                    "(64 hex, or 66 hex ending in 01)"
                )
                logger.warning("Stacks wallet init failed: invalid Stacks key — Stacks payments disabled")

    @property
    def public_key(self) -> str:
        return self.keypair.public_key

    @property
    def total_spent_usdc(self) -> str:
        return str(self._total_spent)

    def get_usdc_balance(self) -> str:
        """Return current USDC balance.

        '0' means genuinely empty (unfunded account / no trustline). An
        unreachable Horizon raises RuntimeError instead of masquerading as
        $0 — otherwise budget_policy() silently clamps the spend cap to
        zero on an infra blip.
        """
        from stellar_sdk.exceptions import NotFoundError
        try:
            account = self.server.load_account(self.public_key)
        except NotFoundError:
            return "0"   # account not on-chain yet — genuinely unfunded
        except Exception as e:
            raise RuntimeError(f"balance check failed (Horizon): {e}") from e
        for b in account.raw_data.get("balances", []):
            if b.get("asset_code") == "USDC":
                return b.get("balance", "0")
        return "0"

    def pay(self, destination: str, amount_usdc: str, memo: str = "") -> dict:
        """
        Send USDC to destination on Stellar.

        Returns:
            {"success": True, "tx_hash": "..."}
            {"success": False, "reason": "..."}

        AGE-68: a submit that TIMES OUT is not a clean failure — Horizon may
        have accepted the transaction while the HTTP response was lost. The tx
        hash is deterministic (computed from the signed envelope before submit),
        so on a timeout-class error we poll Horizon for that exact hash before
        declaring failure. If it landed, we return success with the real hash
        instead of reporting a failure the caller would retry into a double-pay.
        """
        if getattr(self, "stellar_ephemeral", False):
            return {"success": False,
                    "reason": "no Stellar secret configured — this wallet was "
                              "constructed without one (Stacks/Base-only); "
                              "pass secret_key= to pay on Stellar"}
        tx_hash_precomputed = ""
        try:
            account = self.server.load_account(self.public_key)

            builder = TransactionBuilder(
                source_account=account,
                network_passphrase=self.network_passphrase,
                base_fee=100,
            )
            if memo:
                builder.add_text_memo(memo[:28])
            builder.append_payment_op(
                destination=destination,
                asset=self.usdc,
                amount=amount_usdc,
            )
            builder.set_timeout(30)
            tx = builder.build()
            tx.sign(self.keypair)
            # Deterministic hash of the signed envelope — valid to look up on
            # Horizon whether or not submit's response makes it back.
            try:
                tx_hash_precomputed = tx.hash_hex()
            except Exception:
                tx_hash_precomputed = ""
            response = self.server.submit_transaction(tx)

            tx_hash = response.get("hash", "") or tx_hash_precomputed
            self._total_spent += Decimal(amount_usdc)
            logger.info(f"Paid {amount_usdc} USDC → {destination[:8]}... | tx: {tx_hash[:12]}...")

            return {"success": True, "tx_hash": tx_hash}

        except Exception as e:
            reason = _extract_stellar_reason(e)
            # AGE-68: on a timeout/transport-class error, the tx may have landed.
            # Poll Horizon for the precomputed hash before calling it a failure.
            if tx_hash_precomputed and _is_timeout_error(e):
                landed = self._await_tx_on_chain(tx_hash_precomputed)
                if landed:
                    self._total_spent += Decimal(amount_usdc)
                    logger.warning(
                        f"Payment submit timed out but tx CONFIRMED on-chain: "
                        f"{tx_hash_precomputed[:16]}... — treating as success"
                    )
                    return {"success": True, "tx_hash": tx_hash_precomputed}
            logger.error(f"Payment failed: {reason}")
            return {"success": False, "reason": reason}

    def _await_tx_on_chain(self, tx_hash: str, attempts: int = 3, delay: float = 2.0) -> bool:
        """Poll Horizon for a specific tx hash. True once it appears as a
        successful transaction; False if it never shows within the window
        (so the caller can safely treat the payment as not-sent)."""
        import time as _t
        from stellar_sdk.exceptions import NotFoundError
        for i in range(attempts):
            try:
                rec = self.server.transactions().transaction(tx_hash).call()
                if rec.get("successful", True):
                    return True
            except NotFoundError:
                pass
            except Exception as e:
                logger.warning(f"tx poll error for {tx_hash[:16]}...: {e}")
            if i < attempts - 1:
                _t.sleep(delay)
        return False

    def would_exceed_budget(self, amount_usdc: str, max_budget: str) -> bool:
        """Return True if paying this amount would exceed the budget."""
        return (self._total_spent + Decimal(amount_usdc)) > Decimal(max_budget)

    def pay_evm(self, to: str, amount_raw: int) -> dict:
        """
        Send USDC on Base mainnet.

        Args:
            to:          Recipient EVM address (0x...).
            amount_raw:  Amount in USDC smallest unit (6 decimals).
                         e.g. 100000 = $0.10 USDC.

        Returns:
            {"success": True,  "tx_hash": "0x..."}
            {"success": False, "reason": "..."}
        """
        if self._evm_account is None:
            return {
                "success": False,
                "reason": (
                    "Base wallet not configured. Pass base_key= to AgentWallet "
                    "or set BASE_AGENT_KEY env var."
                ),
            }

        try:
            from eth_account import Account as _Account

            # ── Build ERC20 transfer calldata ──────────────────────────────────
            # transfer(address,uint256)
            to_padded     = bytes.fromhex(to.removeprefix("0x").zfill(64))
            amount_padded = amount_raw.to_bytes(32, "big")
            calldata = self._ERC20_TRANSFER_SIG + to_padded + amount_padded

            # ── RPC helpers ────────────────────────────────────────────────────
            def _rpc(method: str, params: list):
                resp = httpx.post(
                    self.BASE_RPC_URL,
                    json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                    timeout=15.0,
                )
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise Exception(f"RPC error: {data['error']}")
                return data["result"]

            nonce     = int(_rpc("eth_getTransactionCount", [self._evm_account.address, "latest"]), 16)
            gas_price = int(_rpc("eth_gasPrice", []), 16)
            gas_limit = 65_000   # ERC20 transfer is ~50k gas; small safety buffer

            tx = {
                "chainId":  self.BASE_CHAIN_ID,
                "nonce":    nonce,
                "to":       self.BASE_USDC,
                "value":    0,
                "data":     "0x" + calldata.hex(),
                "gas":      gas_limit,
                "gasPrice": gas_price,
            }

            signed  = self._evm_account.sign_transaction(tx)
            raw_hex = "0x" + signed.raw_transaction.hex()
            tx_hash = _rpc("eth_sendRawTransaction", [raw_hex])

            self._total_spent += Decimal(amount_raw) / Decimal("1000000")
            logger.info(f"Base payment sent: {amount_raw / 1e6:.6f} USDC → {to[:10]}... | tx: {tx_hash[:16]}...")
            return {"success": True, "tx_hash": tx_hash}

        except Exception as e:
            reason = f"evm:{str(e)[:120]}"
            logger.error(f"Base payment failed: {reason}")
            return {"success": False, "reason": reason}

    def build_base_payment_signature(self, accept: dict, resource_url: str) -> str:
        """
        Sign an EIP-3009 transferWithAuthorization OFF-CHAIN for an x402 Base
        payment option and return the base64 X-PAYMENT payload.

        Crucially, NOTHING is broadcast here. The signed authorization is
        settled server-side by the resource server's facilitator ONLY if the
        request is accepted — so a rejected retry costs nothing. This is the
        gasless x402 v2 flow (the same one the gateway's session_create uses),
        and it fixes the "paid on-chain then rejected" loss that a raw ERC-20
        transfer + tx_hash proof produced against CDP-facilitator tools.

        Args:
            accept:        One entry from the 402 response 'accepts' list
                           (amount, asset, payTo, network, scheme, extra).
            resource_url:  The tool URL being paid for.

        Returns:
            base64-encoded x402 v2 PaymentPayload, ready for the X-PAYMENT
            header.

        Raises:
            RuntimeError:  if no Base wallet is configured.
            ImportError:   if the x402[evm] extra isn't installed.
        """
        if self._evm_account is None:
            raise RuntimeError(
                "Base wallet not configured. Pass base_key= to AgentWallet "
                "or set BASE_AGENT_KEY env var."
            )
        from x402.mechanisms.evm.signers import EthAccountSigner
        from x402.mechanisms.evm.exact.client import ExactEvmScheme
        from x402.schemas import PaymentRequirements

        _atomic = _x402_amount_atomic(accept)
        if _atomic is None:
            # Standard x402 uses maxAmountRequired; AgentPay uses amount. If a
            # 402 carries neither, it's malformed — a clear error beats the old
            # bare KeyError('amount').
            raise KeyError("x402 payment requirements missing amount / maxAmountRequired")
        amount  = str(_atomic)
        asset   = accept.get("asset") or self.BASE_USDC
        # payTo is standard x402; tolerate a pay_to alias, clear error if neither.
        pay_to  = accept.get("payTo") or accept.get("pay_to")
        if not pay_to:
            raise KeyError("x402 payment requirements missing payTo")
        # Live services often advertise 'base' instead of CAIP-2 eip155:8453;
        # the signing lib requires CAIP-2. Normalize before it validates.
        network = _normalize_evm_network(accept.get("network"))
        if not _is_base_settleable(network):
            # Defense in depth: never sign a Base USDC authorization for a chain
            # the facilitator can't settle (Avalanche eip155:43114, Arbitrum
            # eip155:42161, …). Selection already filters these; if one still
            # reaches here, refuse cleanly instead of transmitting a doomed auth
            # that spends but never confirms. (AGE-80)
            raise UnsupportedChainPayment(
                f"cannot settle a Base payment on {network!r} "
                f"(settleable: {sorted(_BASE_SETTLEABLE_CAIP2)})",
                offered_networks=[network],
                settleable=sorted(_BASE_SETTLEABLE_CAIP2),
            )
        scheme_name = accept.get("scheme", "exact")
        # AGE-67/AGE-56: maxTimeoutSeconds comes from the SERVER'S 402 and
        # becomes the signed authorization's validBefore window. Clamp it so a
        # hostile 402 can't request a year-long validity and hold a settleable
        # authorization long after the session/budget is gone.
        timeout = min(int(accept.get("maxTimeoutSeconds", 300)), MAX_AUTH_VALIDITY_SECONDS)
        extra   = accept.get("extra") or {
            "name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009",
        }

        signer = EthAccountSigner(self._evm_account)
        scheme = ExactEvmScheme(signer)
        requirements = PaymentRequirements(
            scheme=scheme_name, network=network, asset=asset, amount=amount,
            pay_to=pay_to, max_timeout_seconds=timeout, extra=extra,
        )
        payload_dict = scheme.create_payment_payload(requirements)

        # AGE-90: `accepted` ECHOES the seller's chosen accepts entry VERBATIM.
        # We used to reconstruct it — normalized network, stringified amount,
        # clamped timeout, plus injected `resource`/`mimeType` keys. Strict v2
        # middlewares deep-compare `accepted` against their own advertised
        # entry, and the injected keys alone produced "No matching payment
        # requirements" → a fresh 402 {} — the 7-seller rejection cluster that
        # capped two prober sweeps. Root-caused live 2026-07-28: removing the
        # two injected keys flips ApiToll/Otto from matcher rejection straight
        # through to signature verification. Tolerant matchers (5-field subset,
        # like x402's own Python server) accept the echo just the same, since
        # it is by definition exactly what the seller advertised. All
        # normalization (CAIP-2 network, amount key, timeout clamp) still
        # applies to the SIGNED authorization above — only the declarative
        # echo is verbatim.
        payment_payload = {
            "x402Version": 2,
            "payload": payload_dict,
            "resource": {"url": resource_url, "mimeType": "application/json"},
            "accepted": json.loads(json.dumps(accept)),   # deep copy, untouched
        }
        return base64.b64encode(json.dumps(payment_payload).encode()).decode()

    # ── Stacks/sBTC payment path (AGE-25) ─────────────────────────────────────

    @property
    def _stacks_api_base(self) -> str:
        import os
        return os.environ.get("STACKS_API_URL") or (
            STACKS_API_MAINNET if self.network == "mainnet" else STACKS_API_TESTNET
        )

    def fetch_stacks_nonce(self) -> int:
        """Next valid account nonce from the Stacks node (`/v2/accounts`)."""
        resp = httpx.get(
            f"{self._stacks_api_base}/v2/accounts/{self.stacks_address}?proof=0",
            timeout=10.0,
        )
        resp.raise_for_status()
        return int(resp.json()["nonce"])

    def build_stacks_payment(self, stacks_opt: dict, payment_id: str, resource_url: str) -> dict:
        """Sign — but DO NOT broadcast — an sBTC transfer for a 402 stacks
        option, and build the lowercase `payment-signature` header payload.

        Sign-don't-broadcast semantics: the return value is a complete signed
        transaction the GATEWAY will broadcast (facilitator /settle, or direct
        Hiro). Once the header leaves the process the tx is live — the caller
        records the spend at transmission, not at HTTP 200 ([CHECKLIST #2]).

        The caller MUST hold self._stacks_lock across sign→transmit→response:
        Stacks nonces are sequential, so exactly one signed tx may be in
        flight per wallet. Nonce = max(chain's next nonce, our local
        successor) — the local successor covers mempool lag right after a
        prior leg settled.

        `stacks_opt` is the `payment_options.stacks` block of AgentPay's 402:
        {amount_sats, amount_usdc, pay_to, network (CAIP-2), fee_microstx?,
        scheme?}. Budget cap math stays in USD (amount_usdc); amount_sats is
        what gets signed. [CHECKLIST #7]'s validity-window clamp has no Stacks
        analog (a signed tx never expires) — the mitigation is this
        serialization plus the gateway's pre-settle replay consume on txid.

        Returns {"header", "txid", "nonce", "amount_sats", "amount_usd"}.
        """
        import os
        from agentpay import _stacks_tx
        if self._stacks_keypair is None:
            raise RuntimeError(
                "Stacks wallet not configured. Pass stacks_key= to AgentWallet "
                "or set STACKS_AGENT_KEY env var."
            )
        network = "mainnet" if self.network == "mainnet" else "testnet"
        expected_caip2 = (
            _stacks_tx.STACKS_MAINNET_CAIP2 if network == "mainnet"
            else _stacks_tx.STACKS_TESTNET_CAIP2
        )
        offered = stacks_opt.get("network") or expected_caip2
        if offered != expected_caip2:
            raise ValueError(
                f"402 stacks option targets {offered} but this wallet is on "
                f"{expected_caip2} — refusing to sign"
            )
        amount_sats = int(stacks_opt["amount_sats"])
        # The cap is enforced in USD; bind it to the sats actually signed.
        _stacks_tx.assert_sats_within_cap(
            amount_sats,
            stacks_opt.get("amount_usdc"),
            stacks_opt.get("btc_usd_rate"),
        )
        pay_to = stacks_opt.get("pay_to") or stacks_opt.get("payTo")
        if not pay_to:
            raise ValueError("402 stacks option has no pay_to address")
        fee = int(
            stacks_opt.get("fee_microstx")
            or os.environ.get("STACKS_FEE_MICROSTX")
            or DEFAULT_STACKS_FEE_MICROSTX
        )
        chain_nonce = self.fetch_stacks_nonce()
        nonce = (
            chain_nonce if self._stacks_next_nonce is None
            else max(chain_nonce, self._stacks_next_nonce)
        )
        unsigned = _stacks_tx.build_sbtc_transfer(
            sender=self._stacks_keypair,
            recipient=pay_to,
            amount_sats=amount_sats,
            payment_id=payment_id,   # [CHECKLIST #5] memo = challenge binding
            nonce=nonce,
            fee_microstx=fee,
            network=network,
        )
        signed = _stacks_tx.sign_transaction(unsigned, self._stacks_keypair)
        txid = _stacks_tx.txid_of(signed)
        payload = {
            "x402Version": 2,
            "scheme": stacks_opt.get("scheme", "exact"),
            "network": expected_caip2,
            # The gateway binds verification to this challenge id: it looks up
            # the pending challenge, then requires the memo INSIDE the signed
            # tx to match it (the memo is the cryptographic binding; this
            # field is the lookup key). docs/stacks-adapter.md §Wire contract.
            "payment_id": payment_id,
            "payload": {"signedTransaction": signed.hex(), "txid": txid},
            "accepted": {
                "scheme": stacks_opt.get("scheme", "exact"),
                "network": expected_caip2,
                "amount": str(amount_sats),
                "asset": "sbtc",
                "payTo": pay_to,
                "resource": resource_url,
                "mimeType": "application/json",
            },
        }
        header = base64.b64encode(json.dumps(payload).encode()).decode()
        return {
            "header": header,
            "txid": txid,
            "nonce": nonce,
            "amount_sats": amount_sats,
            "amount_usd": stacks_opt.get("amount_usdc"),
        }

    def note_stacks_nonce_used(self, nonce: int) -> None:
        """The signed tx carrying `nonce` is live (transmitted) or settled —
        the next leg must sign nonce+1 even if the chain read lags."""
        self._stacks_next_nonce = nonce + 1

    def reset_stacks_nonce(self) -> None:
        """Definitive broadcast rejection (e.g. BadNonce): the signed tx is
        dead and our local successor may be wrong — refetch from chain."""
        self._stacks_next_nonce = None

    def note_stacks_settled(self, amount_usd) -> None:
        """[CHECKLIST #9]: wallet-level spend counter must move for
        sign-don't-broadcast settles too, not only for local broadcasts."""
        try:
            self._total_spent += Decimal(str(amount_usd))
        except (ArithmeticError, ValueError) as e:
            logger.warning(f"note_stacks_settled ignored bad amount {amount_usd!r}: {e}")


# ── Budget-Aware Session ──────────────────────────────────────────────────────

# Default settlement chain for PAID calls when the caller hasn't pinned one.
# Base/EIP-3009 (Mode A) is preferred because it settles through the CDP
# facilitator that keeps AgentPay discoverable on Bazaar; Stellar is the
# automatic fallback when no Base wallet/option is available.
DEFAULT_PAID_CHAIN = "base"

# AGE-53: how much the 402-demanded amount may exceed the registry-quoted
# price before the SDK refuses to pay/sign. Covers rounding/format drift
# ("0.001" vs "0.0010") plus small legitimate repricing; anything larger is
# treated as a hostile or misconfigured gateway and hard-fails pre-payment.
OVERPAY_TOLERANCE = Decimal("0.05")   # 5% relative

# AGE-67/AGE-56: ceiling for the server-controlled maxTimeoutSeconds that
# becomes the EIP-3009 validBefore window. 10 minutes is generous for any
# legitimate settlement; without a clamp a hostile 402 could request a
# year-long window and settle the signed authorization long after the
# session is gone.
MAX_AUTH_VALIDITY_SECONDS = 600


def _x402_amount_atomic(entry: dict):
    """Atomic amount from an x402 payment-requirements ('accepts') entry.

    Tolerates BOTH AgentPay's native `amount` key and the STANDARD x402 v2
    `maxAmountRequired`. Standard-compliant sellers (a growing share of the
    ecosystem) send only `maxAmountRequired`; reading `amount` alone priced
    those options at $0 — so they won the "cheapest" selection — and then
    raised KeyError('amount') at signing. That is the prober's 2026-07-23
    systemic failure, and it hit any agent paying such a URL, not just the
    prober. An explicit `amount` of 0 is honoured (a real free option); only
    a missing/blank `amount` falls through to `maxAmountRequired`.

    Returns the atomic int, or None when neither key is present/parseable
    (callers skip such an option rather than mis-pricing it at $0).
    """
    raw = entry.get("amount")
    if raw is None or raw == "":
        raw = entry.get("maxAmountRequired")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


# Friendly EVM network names → CAIP-2. The x402 signing lib requires CAIP-2
# (eip155:CHAIN_ID); standard x402 uses it too, but many LIVE services
# advertise a friendly name ("base"), which raised
# "Unsupported network format: base (expected eip155:CHAIN_ID)" at signing —
# the prober's 2026-07-23 failure #2, revealed once the amount bug was fixed.
_EVM_NETWORK_CAIP2 = {
    "base": "eip155:8453",
    "base-mainnet": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "base-testnet": "eip155:84532",
}


def _normalize_evm_network(net) -> str:
    """Map a friendly EVM network name to CAIP-2. An already-CAIP-2 value passes
    through; a blank one defaults to Base mainnet (the wallet's chain); an
    unknown non-empty value passes through so the x402 lib can validate it."""
    n = str(net or "").strip().lower()
    if not n:
        return "eip155:8453"
    if n.startswith("eip155:"):
        return n
    return _EVM_NETWORK_CAIP2.get(n, n)


# Base chains this wallet can actually SETTLE on (Base mainnet + sepolia). The
# Base signer builds an EIP-3009 USDC authorization the CDP facilitator settles
# on Base; it cannot settle any other eip155 chain. _chain_kind() historically
# treated ANY eip155:* as Base, so an Avalanche- (eip155:43114) or Arbitrum-
# only (eip155:42161) seller was mistaken for Base: a doomed auth was signed +
# transmitted (real spend, never confirmed) and the seller was mis-scored as a
# delivery failure. (AGE-80)
_BASE_SETTLEABLE_CAIP2 = frozenset({"eip155:8453", "eip155:84532"})


def _is_base_settleable(net) -> bool:
    """True iff `net` is a Base chain this wallet can settle on — Base mainnet
    (8453) / sepolia (84532), or a friendly 'base…' alias. Every other eip155
    chain (Avalanche, Arbitrum, Optimism, Polygon, Solana, …) is False."""
    n = str(net or "").strip().lower()
    if not n:
        return False
    if n.startswith("base"):
        return True
    return _normalize_evm_network(n) in _BASE_SETTLEABLE_CAIP2


def _fmt(amount) -> str:
    """Format a Decimal/str/float as '$0.0030' with clean trailing-zero stripping."""
    s = f"{Decimal(str(amount)):.7f}".rstrip("0").rstrip(".")
    return f"${s}"


class ToolResult(dict):
    """
    The value returned by `Session.call()`.

    It IS the gateway envelope dict (``{"tool", "result", "payment"}``), so all
    existing code keeps working unchanged::

        r = s.call("token_price", {"symbol": "ETH"})
        r["result"]["price_usd"]      # still works

    …but it also adds accessors so you don't have to double-index::

        r.data["price_usd"]           # inner tool output  (== r["result"])
        r.cost                        # payment amount, e.g. "0.001" or "0"
        r.tx                          # settlement tx hash (or None)
        r.network                     # settlement network (or None)

    For third-party x402 tools whose response isn't enveloped, ``.data`` falls
    back to the whole response.
    """

    @property
    def data(self):
        v = self.get("result")
        return v if v is not None else self

    @property
    def _pay(self) -> dict:
        return self.get("payment") or {}

    @property
    def cost(self):
        return self._pay.get("amount_usdc")

    @property
    def tx(self):
        return self._pay.get("tx_hash")

    @property
    def network(self):
        return self._pay.get("network")


def _wrap_result(r):
    """Wrap a gateway/tool response so callers get .data/.cost/.tx ergonomics
    without losing dict behaviour. Non-dicts pass through untouched."""
    return ToolResult(r) if isinstance(r, dict) and not isinstance(r, ToolResult) else r


def _with_query(url: str, params: dict | None) -> str:
    """Merge `params` into `url`'s query string — for GET-served x402 resources.

    A GET resource takes its arguments in the URL, so a POST-shaped params dict
    has nowhere else to go. Before AGE-83 the SDK simply dropped them
    (`client.get(url, headers=...)`), so every GET-served seller was called
    with no arguments at all — it took the payment, then answered with an
    error or an empty body, and looked like a non-deliverer. Live evidence:
    x402.shizu.me/pdf (GET ?url=) scored 0.0 across three paid prober probes
    while being a working service.

    Caller-supplied query params in `url` win over `params` (the caller was
    explicit). Non-scalar values are JSON-encoded; None values are dropped.
    """
    if not params:
        return url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    merged: dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            merged[str(k)] = "true" if v else "false"
        elif isinstance(v, (str, int, float)):
            merged[str(k)] = str(v)
        else:
            merged[str(k)] = json.dumps(v, default=str)
    merged.update(existing)          # explicit URL params are authoritative
    return urlunsplit(parts._replace(query=urlencode(merged)))


def _decode_payment_required_header(headers) -> dict | None:
    """Decode an x402 v2 PAYMENT-REQUIRED (or X-PAYMENT-REQUIRED) header.

    The header carries the payment-required payload as base64-encoded JSON
    (some servers send raw JSON). Returns the payload dict, or None when the
    header is absent/undecodable. `headers` is any case-insensitive mapping
    (httpx.Headers) or a plain dict."""
    raw = None
    try:
        raw = headers.get("PAYMENT-REQUIRED") or headers.get("X-PAYMENT-REQUIRED")
    except Exception:
        pass
    if not raw and isinstance(headers, dict):   # plain dict, unknown casing
        lowered = {str(k).lower(): v for k, v in headers.items()}
        raw = lowered.get("payment-required") or lowered.get("x-payment-required")
    if not raw:
        return None
    for decode in (
        lambda s: json.loads(base64.b64decode(s + "=" * (-len(s) % 4))),
        json.loads,
    ):
        try:
            payload = decode(raw)
            return payload if isinstance(payload, dict) else None
        except Exception:
            continue
    return None


class Session:
    """
    Budget-aware session for multi-tool agent tasks.

    Enforces a hard spend cap across all tool calls. Tool substitution is
    OPT-IN (AGE-118): by default (fallback="off") a call either runs the tool
    you named or raises a typed exception — ToolNotFound for an unknown name,
    BudgetExceeded when it doesn't fit. Pass fallback="auto" to restore
    automatic rerouting to the next-cheapest tool in the same category when
    the budget is tight or the named tool fails before any payment moved.

    Cap semantics under substitution (fallback="auto"): allowed_tools and
    max_per_tool are enforced against the tool ACTUALLY called (the resolved
    target). A max_per_tool cap keyed to the requested name does NOT transfer
    to a substitute — if you cap a tool, cap its plausible substitutes by
    name too, or leave fallback off.

    Usage:
        with Session(wallet, gateway_url, max_spend="0.10") as s:
            price   = s.estimate("token_price")   # "$0.001"
            balance = s.remaining()               # "$0.099"
            result  = s.call("token_price", {"symbol": "ETH"})
            print(s.summary())
    """

    def __init__(
        self,
        wallet: AgentWallet,
        gateway_url: str,
        max_spend: str = "0.10",
        *,
        allowed_tools: list[str] | None = None,
        max_per_tool: dict[str, float] | None = None,
        rate_limit: int | None = None,
        prefer_chain: str | None = None,
        fallback: str = "off",
    ):
        self.wallet = wallet
        self.gateway_url = gateway_url.rstrip("/")
        # Default settlement chain for tools that offer several (e.g. "base",
        # "stellar", or "stacks"). Overridable per-call via call(..., chain=).
        # "stacks" is never a silent default — it only settles when explicitly
        # preferred here or per-call (AGE-25).
        self._prefer_chain = prefer_chain.lower() if prefer_chain else None
        # Coerce through str() so a float cap is EXACT: Decimal(0.10) drifts to
        # 0.1000000000000000055…, but Decimal(str(0.10)) == Decimal("0.10").
        # Accepts "0.10", 0.10, or Decimal("0.10") — all do the right thing.
        self.max_spend = Decimal(str(max_spend))
        self._spent = Decimal("0")
        # AGE-66: guard the budget check→reserve→spend sequence so two threads
        # calling call() concurrently can't both read the full remaining budget
        # over seconds of network I/O and both pay. `_reserved` is the sum of
        # in-flight (broadcast not yet accounted) holds; remaining/would_exceed
        # count it so a second concurrent call sees the money as already
        # committed. Reentrant so the guarded helpers can nest.
        self._lock = threading.RLock()
        self._reserved = Decimal("0")
        self._call_log: list[dict] = []
        self._tool_cache: dict[str, dict] = {}   # tool_name → full tool metadata
        self._all_tools_cache: list[dict] | None = None
        # Policy parameters
        self._allowed_tools: list[str] | None = allowed_tools
        self._max_per_tool: dict[str, Decimal] = {
            k: Decimal(str(v)) for k, v in (max_per_tool or {}).items()
        }
        self._rate_limit: int | None = rate_limit   # max calls per minute
        self._rate_window: list[float] = []          # timestamps of recent calls
        # AGE-118: tool substitution is OPT-IN. "off" (default) = the tool you
        # named or a typed exception; "auto" = legacy behaviour (reroute to the
        # cheapest same-category tool on budget breach or pre-payment failure).
        if fallback not in ("auto", "off"):
            raise ValueError(f'fallback must be "auto" or "off", got {fallback!r}')
        self._fallback = fallback

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._call_log:
            print(self._format_summary())

    # ── Public API ────────────────────────────────────────────────────────────

    def estimate(self, tool_name: str) -> str:
        """
        Query gateway for tool price.
        Returns formatted string like "$0.003", or "unknown".
        """
        info = self._fetch_tool_info(tool_name)
        if info:
            return _fmt(info["price_usdc"])
        return "unknown"

    def remaining(self) -> str:
        """Remaining budget as a formatted DISPLAY string, e.g. '$0.097'.
        For comparisons use remaining_usd() (a Decimal) — comparing the
        '$'-prefixed strings is a foot-gun."""
        return _fmt(self.remaining_usd())

    def remaining_usd(self) -> Decimal:
        """Remaining budget as a Decimal — use this for math/comparisons.
        Counts in-flight reservations (AGE-66) so a concurrent call sees money
        already committed by another thread's in-progress payment."""
        with self._lock:
            return max(self.max_spend - self._spent - self._reserved, Decimal("0"))

    def spent(self) -> str:
        """Total spent so far as a formatted DISPLAY string."""
        return _fmt(self._spent)

    def spent_usd(self) -> Decimal:
        """Total spent so far as a Decimal — use this for math/comparisons."""
        return self._spent

    def would_exceed(self, amount_usdc) -> bool:
        """True if adding this cost would exceed the budget. The recommended
        way to ask "does this fit?" — accepts a str, float, or Decimal.
        Counts in-flight reservations (AGE-66)."""
        with self._lock:
            return (self._spent + self._reserved + Decimal(str(amount_usdc))) > self.max_spend

    def _reserve(self, amount) -> bool:
        """AGE-66: atomically check budget and place a hold. True if the hold
        was placed (call may proceed); False if it wouldn't fit. Paired with
        _absorb_and_release() in a finally after the payment attempt."""
        amt = Decimal(str(amount))
        with self._lock:
            if (self._spent + self._reserved + amt) > self.max_spend:
                return False
            self._reserved += amt
            return True

    def _cap_excluding_hold(self, quote, held) -> str:
        """F1 (2026-07-20): client-side max_spend ceiling for a call whose own
        budget hold is already placed. remaining_usd() subtracts _reserved
        INCLUDING this call's hold, so the old cap double-counted it —
        cap = min(remaining_before − price, 1.05·price) — falsely rejecting
        any call with remaining < 2× price (exact-fit budgets failed; every
        session stranded its last call). Add the hold back, under a single
        lock acquisition so the snapshot is internally consistent.

        Computed AFTER the hold lands, this is race-free w.r.t. this call's
        own hold (the actual bug). Its residual staleness toward holds placed
        after the computation is the one any pre-computed ceiling has —
        bounded by the overpay-tolerance arm and enforced anyway by
        reserve + absorb."""
        q = Decimal(str(quote))
        with self._lock:
            remaining_excl = max(
                self.max_spend - self._spent - self._reserved + Decimal(str(held)),
                Decimal("0"),
            )
        return str(min(remaining_excl, q * (Decimal("1") + OVERPAY_TOLERANCE)))

    def _would_exceed_excluding_hold(self, amount_usdc, held) -> bool:
        """F1 (2026-07-20): like would_exceed(), but ignores `held` — the hold
        THIS call already placed. Used for the fallback fit check, which runs
        while the original hold is still reserved; counting it falsely
        rejected tight-budget fallbacks."""
        with self._lock:
            return (
                self._spent
                + self._reserved
                - Decimal(str(held))
                + Decimal(str(amount_usdc))
            ) > self.max_spend

    def _release(self, amount) -> None:
        """Drop a hold placed by _reserve() (the actual spend is booked
        separately by _absorb_client_log / _call_x402_url)."""
        with self._lock:
            self._reserved = max(self._reserved - Decimal(str(amount)), Decimal("0"))

    def tool_cost(self, tool_name: str) -> str:
        """
        Return the cost of a tool as a formatted DISPLAY string, e.g. '$0.005'
        (or 'unknown'). For deciding whether to call it, use would_exceed()
        or tool_cost_usd() — do NOT compare the '$' strings directly.

        Example (correct):
            if session.would_exceed(session.tool_cost_usd('dune_query')):
                result = session.call('token_price', {...})  # cheaper alternative
        """
        info = self._fetch_tool_info(tool_name)
        if info:
            return _fmt(info["price_usdc"])
        return "unknown"

    def tool_cost_usd(self, tool_name: str) -> Decimal | None:
        """The tool's price as a Decimal (None if unknown) — use for math /
        comparisons / passing to would_exceed()."""
        info = self._fetch_tool_info(tool_name)
        if info and info.get("price_usdc") is not None:
            try:
                return Decimal(str(info["price_usdc"]))
            except (ValueError, ArithmeticError):
                return None
        return None

    def suggest_cheaper(self, tool_name: str) -> dict | None:
        """
        Return the cheapest available tool in the same category as tool_name
        that fits within the remaining budget, excluding tool_name itself.
        Returns a dict with 'name' and 'price', or None if no alternative exists.

        Example:
            alt = session.suggest_cheaper('dune_query')
            if alt:
                result = session.call(alt['name'], params)
        """
        info = self._fetch_tool_info(tool_name)
        category = info.get("category", "data") if info else "data"
        fallback = self._find_fallback(category=category, exclude=tool_name)
        if fallback:
            return {"name": fallback["name"], "price": _fmt(fallback["price_usdc"])}
        return None

    def estimate_plan(self, steps, budget=None) -> dict:
        """Price a multi-step plan BEFORE spending anything.

        Calls the gateway's free POST /v1/plan/estimate — no payment, no
        funded wallet needed. `steps` accepts tool names, (tool, params)
        tuples, or {"tool":..., "params":...} dicts. `budget` defaults to
        this session's remaining budget, so the verdict answers "does this
        plan fit what I have left?".

        Example:
            plan = s.estimate_plan(["token_price", "dune_query", "session_create"])
            if plan["fits_budget"]:
                for step in plan["steps"]:
                    s.call(step["tool"], {...})
        """
        norm = []
        for step in steps:
            if isinstance(step, str):
                norm.append({"tool": step})
            elif isinstance(step, dict):
                norm.append({"tool": step["tool"], "params": step.get("params", {})})
            else:  # (tool, params) tuple/list
                norm.append({
                    "tool": step[0],
                    "params": step[1] if len(step) > 1 else {},
                })
        if budget is None:
            budget = str(self.remaining_usd())
        resp = httpx.post(
            f"{self.gateway_url}/v1/plan/estimate",
            json={"steps": norm, "budget": str(budget)},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def spending_summary(self) -> dict:
        """
        Developer-friendly session receipt — every call, cost, and timestamp.
        Suitable for logging, visibility dashboards, and session receipts.

        Returns:
            {
                "calls": 5,
                "spent": "$0.000",
                "remaining": "$0.100",
                "budget": "$0.100",
                "tools": ["token_price", "whale_activity", ...],
                "breakdown": [{"tool": ..., "cost": ..., "tx_hash": ...}, ...],
            }
        """
        return {
            "calls":     len(self._call_log),
            "spent":     self.spent(),
            "remaining": self.remaining(),
            "budget":    _fmt(self.max_spend),
            "tools":     [e["tool"] for e in self._call_log],
            "breakdown": [
                {
                    "tool":     e["tool"],
                    "cost":     _fmt(e["amount_usdc"]),
                    "tx_hash":  e.get("tx_hash", ""),
                    "network":  e.get("network", "") or "",   # settlement chain
                    # AGE-54: failed/uncertain spends now legitimately appear in
                    # the ledger — receipt consumers must be able to tell them
                    # from settled legs.
                    "success":  e.get("success", True),
                    **({"state": e["state"]} if e.get("state") else {}),
                    **({"fallback_for": e["fallback_for"]} if "fallback_for" in e else {}),
                }
                for e in self._call_log
            ],
        }

    # ── Bazaar discovery ──────────────────────────────────────────────────────

    def discover(
        self,
        query: str,
        max_price_usd: float = None,
        limit: int = 5,
        network: str = None,
    ) -> list[dict]:
        """
        Search the x402 Bazaar for tools matching query, filtered by remaining budget.

        Args:
            query:         Natural language search, e.g. "whale activity" or "web search".
            max_price_usd: Optional price ceiling in USD. Defaults to remaining budget.
            limit:         Max results to return (Bazaar caps at 20).
            network:       Optional CAIP-2 filter, e.g. "eip155:8453" for Base.

        Returns:
            List of dicts, each with:
              "resource"    — the callable URL
              "description" — what the tool does
              "price_usd"   — cheapest payment option in USD
              "network"     — network of the cheapest option
              "accepts"     — full list of payment options

        Example:
            tools = session.discover("whale activity", max_price_usd=0.01)
            print(tools[0]["resource"], tools[0]["price_usd"])
            result = session.call(tools[0]["resource"], {"token": "ETH"})
        """
        remaining_usd = float(self.max_spend - self._spent)
        effective_max = min(
            max_price_usd if max_price_usd is not None else remaining_usd,
            remaining_usd,
        )

        params: dict = {
            "query": query,
            "maxUsdPrice": f"{effective_max:.6f}",
            "limit": min(limit, 20),
        }
        if network:
            params["network"] = network

        try:
            resp = httpx.get(BAZAAR_SEARCH_URL, params=params, timeout=10.0)
            if resp.status_code != 200:
                logger.warning(f"Bazaar search returned {resp.status_code}")
                return []

            resources = resp.json().get("resources", [])
            results = []
            for r in resources:
                accepts = r.get("accepts", [])
                if not accepts:
                    continue

                # Build a clean list of payment options with USD prices
                options = []
                for a in accepts:
                    try:
                        amount_raw = _x402_amount_atomic(a)
                        if amount_raw is None:
                            continue   # no readable price — skip, don't price at $0
                        # AGE-74: Decimal, not binary float, for USDC money.
                        price_usd = Decimal(amount_raw) / Decimal("1000000")
                        options.append({
                            "price_usd":  price_usd,
                            "network":    a.get("network", ""),
                            "pay_to":     a.get("payTo", ""),
                            "asset":      a.get("asset", ""),
                            "scheme":     a.get("scheme", ""),
                            "amount_raw": amount_raw,
                        })
                    except (ValueError, TypeError):
                        continue

                if not options:
                    continue

                cheapest = min(options, key=lambda x: x["price_usd"])
                results.append({
                    "resource":    r.get("resource", ""),
                    "description": r.get("description", ""),
                    "price_usd":   cheapest["price_usd"],
                    "network":     cheapest["network"],
                    "accepts":     options,
                })

            return results

        except Exception as e:
            logger.warning(f"Bazaar discover failed: {e}")
            return []

    def discover_and_call(
        self,
        query: str,
        params: dict = None,
        max_price_usd: float = None,
    ) -> dict:
        """
        Discover the best tool for a query and call it in one step.

        Searches Bazaar, picks the top result within budget, and calls it.
        The agent never needs to know which specific URL was used.

        Example:
            result = session.discover_and_call(
                "solana transaction explanation",
                {"signature": "5KQw..."},
            )
        """
        results = self.discover(query, max_price_usd=max_price_usd, limit=5)
        if not results:
            raise BudgetExceeded(
                f"No tools found on Bazaar for '{query}' "
                f"within remaining budget {self.remaining()}"
            )

        best = results[0]
        logger.info(
            f"[discover] '{query}' → {best['resource']} "
            f"(${best['price_usd']:.4f}, {best['network']})"
        )
        return self.call(best["resource"], params or {})

    # ── External x402 call ────────────────────────────────────────────────────

    def _call_x402_url(self, url: str, params: dict, chain: str | None = None) -> dict:
        """
        Call any external x402-compatible URL directly.

        Handles the full x402 v2 payment flow:
          1. POST to URL
          2. Parse 402 payment requirements from response
          3. Select a Stellar payment option from accepts[]
          4. Pay via Stellar wallet
          5. Retry with X-Payment header (base64 JSON proof)
          6. Record spend in session

        Currently supports Stellar mainnet and testnet.
        Base/Solana support: add EVM wallet to AgentWallet (roadmap).

        NOTE: policy checks (allowed_tools, max_per_tool, rate_limit) are
        enforced by call() BEFORE routing here (AGE-57) — call() is the only
        entry point, so URL targets can no longer bypass the allowlist.
        """
        with httpx.Client(timeout=60.0) as client:
            # ── First request — probe for 402 ─────────────────────────────────
            # Default POST (AgentPay's own tools); GET-only servers (e.g. CMC's
            # DEX endpoints) answer 405 → re-probe with GET.
            logger.info(f"→ x402 external call: {url}")
            try:
                resp = client.post(url, json=params)
                if resp.status_code == 405:
                    # GET-only server: params belong in the query string, not a
                    # discarded body (AGE-83).
                    resp = client.get(_with_query(url, params))
            except Exception as e:
                raise PrePaymentError(f"External x402 call failed: {e}")

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code != 402:
                raise PrePaymentError(
                    f"Expected 200 or 402 from {url}, got {resp.status_code}: {resp.text[:200]}"
                )

            # ── Parse payment options (x402 v2 'accepts[]') ───────────────────
            try:
                data = resp.json()
            except Exception:
                data = None
            # x402 v2: requirements also (or ONLY) ride the PAYMENT-REQUIRED
            # header as base64 JSON — many sellers send an empty/minimal body
            # (first prober sweep 2026-07-10: 10/15 live 402s were header-only).
            # Non-empty body keys win over the header's.
            hdr_payload = _decode_payment_required_header(resp.headers)
            if not isinstance(data, dict):
                if hdr_payload is None:
                    raise PrePaymentError(f"Could not parse 402 response from {url}: {resp.text[:200]}")
                data = hdr_payload
            elif hdr_payload and not (data.get("accepts") or []):
                data = {**hdr_payload, **{k: v for k, v in data.items() if v}}
            # The signed payment's `resource` MUST match what the server declared
            # in its 402, not our request URL. Servers like CMC declare the bare
            # path (…/dex/search) while we request with query params (…?q=BNB) —
            # signing the request URL → "resource ... does not match" rejection.
            # Prefer the 402's resource.url; fall back to the query-stripped URL.
            resource_for_payment = (data.get("resource") or {}).get("url") or url.split("?", 1)[0]
            # HTTP method the server serves the resource with. CMC's DEX endpoints
            # declare GET (query params in the URL); AgentPay's own tools use POST.
            # Read it from the 402's bazaar extension; default POST.
            req_method = "POST"
            try:
                _inp = (((data.get("extensions") or {}).get("bazaar") or {}).get("info") or {}).get("input") or {}
                _m = str(_inp.get("method") or "").upper()
                if _m in ("GET", "POST"):
                    req_method = _m
            except Exception:
                pass
            accepts = data.get("accepts", []) or []
            if not accepts:
                # AgentPay's own endpoints use the native 'payment_options' shape,
                # not x402-v2 'accepts' — guide the caller instead of failing cryptically.
                if data.get("payment_options"):
                    raise PrePaymentError(
                        f"{url} returned an AgentPay-native 402 (payment_options, not "
                        f"x402-v2 'accepts'). Call AgentPay tools by name — "
                        f"session.call('tool_name') — rather than by URL."
                    )
                raise PrePaymentError(f"402 from {url} had no payment requirements in 'accepts'")

            # ── Normalise into payable candidates, tagged by chain ────────────
            def _chain_kind(net) -> str | None:
                n = str(net or "").lower()
                # Base is settleable ONLY on Base chain-ids (8453 / 84532), not
                # every eip155:* — an Avalanche/Arbitrum-only seller must not be
                # mistaken for Base. (AGE-80)
                if _is_base_settleable(n):
                    return "base"
                if "stellar" in n:
                    return "stellar"
                return None

            candidates = []
            for a in accepts:
                kind_ = _chain_kind(a.get("network"))
                if kind_ is None:
                    continue
                atomic = _x402_amount_atomic(a)
                if atomic is None:
                    continue
                can = bool(self.wallet.base_address) if kind_ == "base" else True  # any Stellar wallet can pay
                candidates.append({
                    "kind": kind_, "network": a.get("network", ""), "pay_to": a.get("payTo"),
                    # AGE-74: Decimal, not float, for the USDC amount string.
                    "amount_atomic": atomic,
                    "amount_usdc": f"{Decimal(atomic) / Decimal('1000000'):.6f}",
                    "scheme": a.get("scheme", "exact"), "accept": a, "payable": can,
                })

            payable_opts = [c for c in candidates if c["payable"]]
            wallet_can = (["base"] if self.wallet.base_address else []) + ["stellar"]

            # ── Select by policy ──────────────────────────────────────────────
            # Explicit chain (per-call chain= or Session prefer_chain=) is a hard
            # requirement → raise if not payable. With no explicit pin we default
            # to Base (Mode A / Bazaar-indexable) when it's payable, otherwise the
            # cheapest payable option (Stellar fallback).
            want = (chain or self._prefer_chain)
            want = want.lower() if want else None
            if want:
                match = [c for c in payable_opts if c["kind"] == want]
                if not match:
                    offered = sorted({c["kind"] for c in candidates})
                    raise PaymentFailed(
                        f"chain='{want}' is not usable for {url}. Tool offers "
                        f"{offered or 'no recognised chains'}; your wallet can pay "
                        f"{sorted(set(wallet_can))}."
                    )
                chosen = min(match, key=lambda c: c["amount_atomic"])
            elif payable_opts:
                base_payable = [c for c in payable_opts if c["kind"] == DEFAULT_PAID_CHAIN]
                pool = base_payable or payable_opts
                chosen = min(pool, key=lambda c: c["amount_atomic"])
            else:
                if not candidates:
                    # No advertised option is on a chain AgentPay can settle at
                    # all (distinct from a missing-key case). Unmet demand on an
                    # unsupported chain, NOT a settlement failure — surface the
                    # offered networks structurally so the prober records the
                    # chain instead of scoring the seller as a failure. (AGE-80)
                    unsettleable = sorted({str(a.get("network", "?")) for a in accepts})
                    raise UnsupportedChainPayment(
                        f"{url} requires payment on {unsettleable}, none of which "
                        f"AgentPay can settle (settleable: {sorted(set(wallet_can))}).",
                        offered_networks=unsettleable,
                        settleable=sorted(set(wallet_can)),
                    )
                offered = sorted({c["kind"] for c in candidates})
                raise PaymentFailed(
                    f"{url} requires payment on {offered}, but your wallet can only pay "
                    f"on {sorted(set(wallet_can))}. Add a Base key (base_key= / "
                    f"BASE_AGENT_KEY) to pay on Base."
                )

            kind        = chosen["kind"]
            base_accept = chosen["accept"]
            amount_usdc = chosen["amount_usdc"]
            pay_to      = chosen["pay_to"]
            pay_network = chosen["network"]
            pay_scheme  = chosen["scheme"]

            # ── Budget check + atomic reservation (AGE-66) ────────────────────
            if not self._reserve(amount_usdc):
                raise BudgetExceeded(
                    f"Tool costs ${float(amount_usdc):.4f} but only "
                    f"{self.remaining()} remains (budget: {_fmt(self.max_spend)})"
                )
            _url_reserved = Decimal(str(amount_usdc))

            # ── Pay on the selected network and retry ─────────────────────────
            # AGE-54/AGE-56: spend is recorded the moment value can leave the
            # wallet — at Stellar broadcast, or at Base auth transmission — NOT
            # when the call returns 200. A pay-then-fail loop must burn budget.
            tx_hash = ""
            entry = {
                "tool":        url,
                "amount_usdc": amount_usdc,
                "tx_hash":     "",
                "network":     pay_network,
                "success":     False,
                "external":    True,
            }

            def _record_spend(state: str):
                # AGE-66: book the spend and drop the hold atomically.
                entry["state"] = state
                with self._lock:
                    self._spent += Decimal(amount_usdc)
                    self._reserved = max(self._reserved - _url_reserved, Decimal("0"))
                    self._call_log.append(entry)

            if kind == "base":
                # Base: sign EIP-3009 OFF-CHAIN — nothing is broadcast here. The
                # resource server's facilitator settles the authorization if it
                # accepts the request. Signing failures are pre-payment; but the
                # moment the POST carrying the auth leaves the wire, the server
                # holds a signature it CAN settle within validBefore — so any
                # failure after transmission is treated as potentially spent
                # (AGE-56), never as "no payment settled".
                logger.info(f"  402 — signing {amount_usdc} USDC auth for {pay_to[:10]}... (Base, off-chain)")
                try:
                    x_payment = self.wallet.build_base_payment_signature(base_accept, resource_for_payment)
                except Exception as e:
                    self._release(_url_reserved)   # AGE-66: pre-payment, no funds moved
                    raise PaymentFailed(f"evm:could not sign x402 payment: {str(e)[:160]}")
                payer_address = self.wallet.base_address

                _headers = {
                    "X-PAYMENT":         x_payment,   # x402 v2 standard header
                    "PAYMENT-SIGNATURE": x_payment,   # alias some gateways use
                    "X-Agent-Address":   payer_address,
                }
                # Recorded BEFORE transmission: if the request itself times out,
                # the auth may still have reached the server.
                _record_spend("signed_auth_transmitted")
                try:
                    # GET: arguments ride the query string (AGE-83). The signed
                    # `resource` is resource_for_payment (query-stripped), so
                    # adding params here can't break the signature match.
                    retry = (client.get(_with_query(url, params), headers=_headers)
                             if req_method == "GET"
                             else client.post(url, json=params, headers=_headers))
                except Exception as e:
                    entry["state"] = "uncertain_settlement"
                    raise Exception(
                        f"External x402 call errored after the signed authorization "
                        f"was transmitted — settlement uncertain, spend recorded: {e}"
                    )
                if retry.status_code != 200:
                    # The server rejected the call but STILL holds a valid signed
                    # authorization it could settle within validBefore. Count the
                    # spend (fail closed) instead of claiming nothing was paid.
                    entry["state"] = "uncertain_settlement"
                    raise Exception(
                        f"External x402 call rejected after auth transmission "
                        f"(settlement uncertain, spend recorded): "
                        f"{retry.status_code} {retry.text[:200]}"
                    )
                result = retry.json()
                if isinstance(result, dict):
                    tx_hash = ((result.get("payment") or {}).get("tx_hash")) or ""
                    entry["tx_hash"] = tx_hash
            else:
                # Stellar: broadcast the payment, then prove it with the tx_hash.
                # AGE-74: bind the memo to this call (resource + fresh nonce)
                # instead of a constant "agentpay-x402" — makes the on-chain
                # record attributable to the specific request and non-replayable
                # as a generic marker. Stellar text memos are ≤28 bytes.
                _memo = f"ap:{secrets.token_hex(8)}"[:28]
                logger.info(f"  402 — paying {amount_usdc} USDC to {pay_to[:10]}... (Stellar, memo={_memo})")
                payment = self.wallet.pay(
                    destination=pay_to, amount_usdc=amount_usdc, memo=_memo,
                )
                if not payment["success"]:
                    self._release(_url_reserved)   # AGE-66: pre-payment, no funds moved
                    raise PaymentFailed(payment["reason"])
                tx_hash = payment["tx_hash"]
                entry["tx_hash"] = tx_hash
                payer_address = self.wallet.public_key
                logger.info(f"  ✓ Payment sent | tx: {tx_hash[:16]}...")
                # Funds have LEFT the wallet — record now, regardless of what
                # the retry returns (AGE-54).
                _record_spend("paid_awaiting_result")

                proof_payload = {
                    "x402Version": 2,
                    "scheme":      pay_scheme,
                    "network":     pay_network,
                    "payload":     {"signature": tx_hash, "from": payer_address},
                }
                x_payment = base64.b64encode(json.dumps(proof_payload).encode()).decode()
                _headers = {"X-Payment": x_payment, "X-Agent-Address": payer_address}
                try:
                    retry = (client.get(_with_query(url, params), headers=_headers)
                             if req_method == "GET"
                             else client.post(url, json=params, headers=_headers))
                except Exception as e:
                    entry["state"] = "paid_no_result"
                    raise Exception(
                        f"External x402 call errored after payment "
                        f"(spend recorded): {e}"
                    )
                if retry.status_code != 200:
                    entry["state"] = "paid_no_result"
                    raise Exception(
                        f"External x402 call failed after payment (spend recorded): "
                        f"{retry.status_code} {retry.text[:200]}"
                    )
                result = retry.json()

            # ── Success: mark the already-recorded spend entry settled ─────────
            cost = Decimal(amount_usdc)
            entry["success"] = True
            entry["state"] = "settled"
            # Make the settlement chain observable on the result (ToolResult.network)
            # for third-party tools whose response isn't already enveloped.
            if isinstance(result, dict) and "payment" not in result:
                result["payment"] = {"amount_usdc": amount_usdc, "tx_hash": tx_hash, "network": pay_network}

            logger.info(f"  ✓ External x402 call complete on {pay_network} | spent {_fmt(cost)}")
            return result

    def call(self, tool_name: str, params: dict = None, *, chain: str | None = None) -> dict:
        """
        Call a paid tool within budget.

        Accepts either:
          - A tool name from AgentPay's registry ("token_price", "whale_activity", ...)
          - Any external x402-compatible URL ("https://api.oatp.cc/tools/tx_explainer")

        For external URLs that offer payment on several chains, `chain=` ("base"
        or "stellar") picks which to settle on; for AgentPay registry tools
        `chain="stacks"` selects sBTC settlement over the Stacks x402 rail
        (sign-don't-broadcast; requires stacks_key= on the wallet). Without
        chain=, the Session's
        prefer_chain (or cheapest payable option) is used. The chosen chain is
        recorded on the result (``.network``) and the receipt.

        For external URLs, payment goes directly to the tool provider.
        AgentPay tracks the spend locally and enforces the budget cap.
        NOTE: an external URL has no registry quote, so a URL call is bounded
        only by the remaining session budget (its 402 IS the quote). To bound
        spend on a specific URL, use max_per_tool={"https://…": cap}.

        - Pre-checks the price against remaining budget.
        - Raises ToolNotFound for an unknown registry tool name (never
          substituted — a typo is an input error, not a budget problem;
          AGE-118).
        - With Session(fallback="auto") only: if budget would be exceeded, or
          the tool fails BEFORE any payment moved, reroutes to the
          next-cheapest tool in the same category that fits. Failures after
          funds moved (or after a signed authorization was transmitted) are
          never retried with a second payment. With the default
          fallback="off", no substitution ever happens.
        - Raises BudgetExceeded if no affordable option exists, or if the 402
          demands more than the quoted price allows.
        - Records actual spend from the x402 payment receipt — including
          payments whose tool call then failed.
        """
        from agentpay._client import AgentPayClient

        params = params or {}

        # ── Policy gate (AGE-57): allowlist, rate limit, per-tool cap apply to
        # BOTH registry tools and external x402 URLs, BEFORE any routing.
        self._check_call_policies(tool_name)

        # ── External x402 URL: route directly, skip AgentPay registry ─────────
        if isinstance(tool_name, str) and tool_name.startswith(("http://", "https://")):
            return _wrap_result(self._call_x402_url(tool_name, params, chain=chain))

        # ── Resolve paid-tool chain preference (Base default, Stellar fallback)
        # An explicit chain (per-call chain= or Session prefer_chain=) is a hard
        # requirement; otherwise we default to Base so paid AgentPay settlements
        # flow through the CDP/Mode-A path that keeps the Bazaar listing live.
        _chain_is_explicit = bool(chain or self._prefer_chain)
        _prefer_chain = (chain or self._prefer_chain or DEFAULT_PAID_CHAIN).lower()

        # ── Resolve which tool to actually call ───────────────────────────────
        # AGE-118: an unknown tool name is an input error, full stop. It is
        # never substituted (even with fallback="auto" — there is no category
        # to substitute within; the old behaviour silently billed an unrelated
        # category="data" tool for a typo) and it is not a budget problem, so
        # it raises the typed ToolNotFound rather than BudgetExceeded.
        tool_info = self._fetch_tool_info(tool_name)
        if tool_info is None:
            raise ToolNotFound(
                f"Tool '{tool_name}' not found on gateway {self.gateway_url} — "
                f"check the name against GET {self.gateway_url}/tools"
            )

        price = tool_info["price_usdc"]
        target = tool_name

        if self.would_exceed(price):
            # AGE-118: budget-breach rerouting is opt-in (fallback="auto").
            # Default "off" keeps budget semantics predictable: the tool you
            # named either fits or the call raises BudgetExceeded.
            fallback = None
            if self._fallback == "auto":
                category = tool_info.get("category", "data")
                fallback = self._find_fallback(category=category, exclude=target)
            if fallback and not self.would_exceed(fallback["price_usdc"]):
                logger.info(
                    f"  [budget] '{target}' costs {_fmt(price)}, "
                    f"remaining {self.remaining()} — "
                    f"falling back to '{fallback['name']}' ({_fmt(fallback['price_usdc'])})"
                )
                target = fallback["name"]
                price = fallback["price_usdc"]
            else:
                raise BudgetExceeded(
                    f"'{tool_name}' costs {_fmt(price)} but only "
                    f"{self.remaining()} remains (budget: {_fmt(self.max_spend)})"
                )

        # ── Execute via x402 flow ─────────────────────────────────────────────
        # AGE-53: the cap handed to the client binds the amount ACTUALLY
        # demanded by the 402, not just the registry-advertised price: never
        # more than the remaining session budget, and never more than the
        # quoted price plus a small overpay tolerance. The client hard-fails
        # BEFORE paying or signing if the 402 demands more.
        # F1 (2026-07-20): the cap is computed AFTER this call's own hold is
        # placed, so it must ADD THE HOLD BACK — see _cap_excluding_hold.
        # (The old remaining_usd()-based cap double-counted the hold: cap =
        # min(remaining_before − price, 1.05·price), so exact-fit budgets
        # failed and every session silently stranded its last call.)

        # AGE-74: per-tool cap as a would-EXCEED check on the RESOLVED target,
        # not the floor check in _check_call_policies (which only blocks the
        # NEXT call once already-spent ≥ cap, letting the call that crosses the
        # cap through, and is keyed on the requested name so a fallback escapes
        # it). Here we know the real target + price.
        if target in self._max_per_tool:
            already = sum(
                Decimal(e["amount_usdc"]) for e in self._call_log if e["tool"] == target
            )
            if already + Decimal(str(price)) > self._max_per_tool[target]:
                raise BudgetExceeded(
                    f"Per-tool cap for '{target}': this call ({_fmt(price)}) would "
                    f"bring spend to {_fmt(already + Decimal(str(price)))}, over the "
                    f"{_fmt(self._max_per_tool[target])} cap"
                )

        # AGE-66: place an atomic budget hold before any funds can move. Even
        # if two threads both cleared would_exceed above, only one gets the
        # reservation; the loser fails closed rather than double-paying.
        if not self._reserve(price):
            raise BudgetExceeded(
                f"'{target}' costs {_fmt(price)} but the remaining budget was "
                f"just consumed by a concurrent call ({self.remaining()} left)"
            )
        reserved_amt = Decimal(str(price))

        client = AgentPayClient(wallet=self.wallet, gateway_url=self.gateway_url)
        try:
            try:
                result = client.call_tool(
                    target, params,
                    max_spend=self._cap_excluding_hold(price, reserved_amt),
                    prefer_chain=_prefer_chain, chain_is_explicit=_chain_is_explicit,
                )
            except (PaymentFailed, RefundPending):
                # PaymentFailed: the on-chain payment itself failed — a fallback
                # tool would fail for the same reason (empty wallet, wrong
                # network, etc.). RefundPending: the agent already paid and the
                # gateway queued a refund — falling back would spend more USDC
                # on the same upstream failure mode. Surface the typed
                # exceptions so callers can branch on them explicitly.
                raise
            except PrePaymentError as exc:
                # AGE-55: fall back ONLY when no funds moved and no signed
                # authorization left the process. Any other exception (e.g.
                # "Tool call failed after payment", transport errors during the
                # paid retry) is treated as potentially-paid and re-raised —
                # a fallback there would be a second payment.
                if target != tool_name:
                    raise
                # AGE-118: pre-payment-failure rerouting is the same class of
                # silent substitution — gated on the same opt-in switch.
                if self._fallback != "auto":
                    raise
                category = tool_info.get("category", "data")
                fallback = self._find_fallback(category=category, exclude=target)
                # F1 (2026-07-20): the fit check runs while THIS call's
                # original hold is still reserved, so it must exclude it —
                # would_exceed() counts the hold and falsely rejected
                # tight-budget fallbacks. The _reserve below stays the
                # authoritative (fail-closed) check.
                if not (
                    fallback
                    and not self._would_exceed_excluding_hold(
                        fallback["price_usdc"], reserved_amt
                    )
                ):
                    raise
                logger.warning(
                    f"  '{target}' failed pre-payment ({exc}) — trying '{fallback['name']}'"
                )
                # Keep the failed leg's (necessarily $0) entries on the receipt
                # before the client is replaced — full session visibility.
                self._absorb_client_log(client, requested=tool_name, target=target)
                # ...and clear the absorbed entries so the `finally` can't fold
                # them in a second time if the re-reserve below raises (dup
                # $0 receipt rows — follow-up review low, 2026-07-20).
                client.call_log.clear()
                # Re-point the hold at the fallback price (AGE-66). Zero
                # reserved_amt the instant the release lands: if the re-reserve
                # below raises BudgetExceeded, the `finally` must NOT release the
                # original hold a second time. A double-release drives _reserved
                # negative and, under concurrent Session.call, lets the budget be
                # overspent by a leg price (adversarial review finding, 2026-07).
                self._release(reserved_amt)
                reserved_amt = Decimal("0")
                if not self._reserve(fallback["price_usdc"]):
                    raise BudgetExceeded(
                        f"fallback '{fallback['name']}' no longer fits the "
                        f"remaining budget ({self.remaining()} left)"
                    )
                reserved_amt = Decimal(str(fallback["price_usdc"]))
                # Set target BEFORE the call so a fallback leg that pays and
                # then fails still gets its fallback_for tag in the finally.
                target = fallback["name"]
                client = AgentPayClient(wallet=self.wallet, gateway_url=self.gateway_url)
                result = client.call_tool(
                    target, params,
                    max_spend=self._cap_excluding_hold(
                        fallback["price_usdc"], reserved_amt
                    ),
                    prefer_chain=_prefer_chain, chain_is_explicit=_chain_is_explicit,
                )
        finally:
            # AGE-54 + AGE-66 + F2 (2026-07-20): fold EVERY payment the client
            # made into the session (success or failure — a broadcast payment
            # whose tool call then failed still burned budget) AND drop this
            # call's hold, in ONE locked section. The previous two-step
            # release-then-absorb left a window where _reserved was already
            # decremented but _spent not yet incremented, so a concurrent
            # _reserve saw inflated remaining and could over-commit the budget
            # by up to one leg price. Mirrors the URL path's _record_spend.
            self._absorb_and_release(
                client, requested=tool_name, target=target, held=reserved_amt
            )

        # Settlement chain from the gateway receipt (e.g. 'stellar-mainnet',
        # 'base', or 'free' for $0 tools) — recorded so it shows on the receipt.
        if isinstance(result, dict) and self._call_log:
            net = (result.get("payment") or {}).get("network", "") or ""
            if net and not self._call_log[-1].get("network"):
                self._call_log[-1]["network"] = net

        return _wrap_result(result)

    def _check_call_policies(self, name_or_url: str) -> None:
        """Pre-payment policy gate shared by registry tools AND external x402
        URLs (AGE-57): allowed_tools allowlist, rate limit, per-tool cap.
        call() is the single entry point, so URL targets can no longer bypass
        the allowlist by skipping the registry path. AGE-66: rate-window and
        per-tool-cap reads run under the session lock so concurrent calls
        can't both slip past the same limit."""
        import time as _time

        if self._allowed_tools is not None and name_or_url not in self._allowed_tools:
            raise BudgetExceeded(
                f"Tool '{name_or_url}' is not in the session allowlist: {self._allowed_tools}"
            )

        with self._lock:
            if self._rate_limit is not None:
                now = _time.monotonic()
                # Prune calls older than 60 seconds
                self._rate_window = [t for t in self._rate_window if now - t < 60.0]
                if len(self._rate_window) >= self._rate_limit:
                    raise BudgetExceeded(
                        f"Rate limit exceeded: max {self._rate_limit} calls/min "
                        f"(made {len(self._rate_window)} in the last 60s)"
                    )
                self._rate_window.append(now)

        if name_or_url in self._max_per_tool:
            already_spent_on_tool = sum(
                Decimal(e["amount_usdc"])
                for e in self._call_log
                if e["tool"] == name_or_url
            )
            if already_spent_on_tool >= self._max_per_tool[name_or_url]:
                raise BudgetExceeded(
                    f"Per-tool cap reached for '{name_or_url}': "
                    f"spent {_fmt(already_spent_on_tool)} of max {_fmt(self._max_per_tool[name_or_url])}"
                )

    def _absorb_client_log(self, client, requested: str, target: str) -> None:
        """Fold an AgentPayClient's call_log into the session ledger and
        budget. Runs on success AND failure paths (AGE-54): the client records
        an entry the moment value can leave the wallet, so every broadcast
        payment counts against the cap even when the tool call then failed.
        AGE-66: _spent/_call_log mutations run under the session lock."""
        with self._lock:
            self._absorb_client_log_locked(client, requested, target)

    def _absorb_and_release(self, client, requested: str, target: str, held) -> None:
        """F2 (2026-07-20): book the client's spend AND drop this call's
        budget hold in ONE locked section — mirroring the URL path's
        _record_spend. Absorb-before-release inside the same lock means no
        observer can ever see the hold gone while the spend is unbooked
        (the overspend direction); the momentary spent+held double-count is
        impossible too, since both mutations commit atomically."""
        with self._lock:
            self._absorb_client_log_locked(client, requested, target)
            self._reserved = max(self._reserved - Decimal(str(held)), Decimal("0"))

    def _absorb_client_log_locked(self, client, requested: str, target: str) -> None:
        """Core of _absorb_client_log — caller MUST hold self._lock."""
        for e in client.call_log:
            cost = Decimal(str(e.get("amount_usdc", "0")))
            self._spent += cost
            entry: dict = {
                "tool":        e.get("tool", target),
                "amount_usdc": str(cost),
                "tx_hash":     e.get("tx_hash", "") or "",
                "network":     e.get("network", "") or "",
                "success":     bool(e.get("success")),
            }
            if e.get("state"):
                entry["state"] = e["state"]
            if target != requested and entry["tool"] == target:
                entry["fallback_for"] = requested
            self._call_log.append(entry)
            if entry["success"]:
                self._tool_cache.setdefault(entry["tool"], {})["price_usdc"] = str(cost)

    def summary(self) -> dict:
        return {
            "calls": len(self._call_log),
            "spent_usdc": str(self._spent),
            "spent_fmt": self.spent(),
            "remaining_usdc": str(max(self.max_spend - self._spent, Decimal("0"))),
            "remaining_fmt": self.remaining(),
            "max_spend_usdc": str(self.max_spend),
            "breakdown": self._call_log,
        }

    def print_summary(self):
        print(self._format_summary())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _fetch_tool_info(self, tool_name: str) -> dict | None:
        """Fetch and cache tool metadata from gateway."""
        if tool_name in self._tool_cache:
            return self._tool_cache[tool_name]
        try:
            resp = httpx.get(f"{self.gateway_url}/tools/{tool_name}", timeout=5.0)
            if resp.status_code == 200:
                info = resp.json()
                self._tool_cache[tool_name] = info
                return info
        except Exception:
            pass
        return None

    def _all_tools(self) -> list[dict]:
        """Fetch and cache the full tool list from gateway."""
        if self._all_tools_cache is not None:
            return self._all_tools_cache
        try:
            resp = httpx.get(f"{self.gateway_url}/tools", timeout=5.0)
            if resp.status_code == 200:
                self._all_tools_cache = resp.json().get("tools", [])
                return self._all_tools_cache
        except Exception:
            pass
        return []

    def _find_fallback(self, category: str, exclude: str) -> dict | None:
        """
        Find the cheapest available tool in `category` within remaining budget,
        excluding `exclude`. Returns tool dict or None.

        Policy-aware (AGE-57): a fallback must satisfy the same session
        policies as a named tool — the SDK picking it instead of the caller
        does not exempt it from the allowed_tools allowlist or a per-tool cap.
        """
        remaining = self.max_spend - self._spent

        def _policy_ok(t: dict) -> bool:
            name = t.get("name")
            if self._allowed_tools is not None and name not in self._allowed_tools:
                return False
            if name in self._max_per_tool:
                already = sum(
                    Decimal(e["amount_usdc"])
                    for e in self._call_log
                    if e["tool"] == name
                )
                if already >= self._max_per_tool[name]:
                    return False
            return True

        candidates = [
            t for t in self._all_tools()
            if t.get("category") == category
            and t.get("name") != exclude
            and t.get("active", True)
            and Decimal(t.get("price_usdc", "999")) <= remaining
            and _policy_ok(t)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda t: Decimal(t["price_usdc"]))

    def _format_summary(self) -> str:
        width = 58
        lines = [
            "",
            "─" * width,
            "  AgentPay Session Summary",
            "─" * width,
            f"  Calls made:  {len(self._call_log)}",
            f"  Spent:       {self.spent()}  (budget: {_fmt(self.max_spend)})",
            f"  Remaining:   {self.remaining()}",
            "",
            "  Breakdown:",
        ]
        for entry in self._call_log:
            tx = (entry.get("tx_hash") or "")[:16]
            net = entry.get("network") or ""
            label = entry["tool"]
            if "fallback_for" in entry:
                label += f"  (fallback for {entry['fallback_for']})"
            lines.append(
                f"    {label:<30} {_fmt(entry['amount_usdc']):>9}"
                + (f"  {net}" if net else "")
                + (f"  |  {tx}..." if tx else "")
            )
        lines.append("─" * width)
        return "\n".join(lines)


# Backwards-compatible alias
BudgetSession = Session
