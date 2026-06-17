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
USDC_ISSUER_TESTNET = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
USDC_ISSUER_MAINNET = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"


# ── Exceptions ────────────────────────────────────────────────────────────────

class BudgetExceeded(Exception):
    """Raised when a tool call would exceed the session budget."""
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
        secret_key:   Stellar secret key (S...).
        network:      "mainnet" or "testnet" (applies to Stellar).
        base_key:     Optional Base/EVM private key (0x...) for paying
                      x402 tools that only accept Base USDC.
                      Read from env var BASE_AGENT_KEY if not passed.

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

    def __init__(self, secret_key: str, network: str = "testnet", *, base_key: str = None):
        import os
        self.keypair = Keypair.from_secret(secret_key)
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
            except Exception as e:
                self.base_disabled_reason = f"Base key rejected: {str(e)[:120]}"
                logger.warning(f"Base wallet init failed: {e} — Base payments disabled")
                self._evm_account = None
                self.base_address = None
        else:
            self._evm_account = None
            self.base_address = None

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
        """
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
            response = self.server.submit_transaction(tx)

            tx_hash = response.get("hash", "")
            self._total_spent += Decimal(amount_usdc)
            logger.info(f"Paid {amount_usdc} USDC → {destination[:8]}... | tx: {tx_hash[:12]}...")

            return {"success": True, "tx_hash": tx_hash}

        except Exception as e:
            reason = _extract_stellar_reason(e)
            logger.error(f"Payment failed: {reason}")
            return {"success": False, "reason": reason}

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

        amount  = str(accept["amount"])
        asset   = accept.get("asset") or self.BASE_USDC
        pay_to  = accept["payTo"]
        network = accept.get("network", "eip155:8453")
        scheme_name = accept.get("scheme", "exact")
        timeout = int(accept.get("maxTimeoutSeconds", 300))
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

        payment_payload = {
            "x402Version": 2,
            "payload": payload_dict,
            "resource": {"url": resource_url, "mimeType": "application/json"},
            "accepted": {
                "scheme": scheme_name, "network": network, "amount": amount,
                "asset": asset, "payTo": pay_to, "maxTimeoutSeconds": timeout,
                "resource": resource_url, "mimeType": "application/json",
                "extra": extra,
            },
        }
        return base64.b64encode(json.dumps(payment_payload).encode()).decode()


# ── Budget-Aware Session ──────────────────────────────────────────────────────

# Default settlement chain for PAID calls when the caller hasn't pinned one.
# Base/EIP-3009 (Mode A) is preferred because it settles through the CDP
# facilitator that keeps AgentPay discoverable on Bazaar; Stellar is the
# automatic fallback when no Base wallet/option is available.
DEFAULT_PAID_CHAIN = "base"


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


class Session:
    """
    Budget-aware session for multi-tool agent tasks.

    Enforces a hard spend cap across all tool calls, with automatic fallback
    to the next-cheapest tool in the same category when budget is tight or a
    tool is unavailable.

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
    ):
        self.wallet = wallet
        self.gateway_url = gateway_url.rstrip("/")
        # Default settlement chain for external x402 tools that offer several
        # (e.g. "base" or "stellar"). Overridable per-call via call(..., chain=).
        self._prefer_chain = prefer_chain.lower() if prefer_chain else None
        # Coerce through str() so a float cap is EXACT: Decimal(0.10) drifts to
        # 0.1000000000000000055…, but Decimal(str(0.10)) == Decimal("0.10").
        # Accepts "0.10", 0.10, or Decimal("0.10") — all do the right thing.
        self.max_spend = Decimal(str(max_spend))
        self._spent = Decimal("0")
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
        """Remaining budget as a Decimal — use this for math/comparisons."""
        return max(self.max_spend - self._spent, Decimal("0"))

    def spent(self) -> str:
        """Total spent so far as a formatted DISPLAY string."""
        return _fmt(self._spent)

    def spent_usd(self) -> Decimal:
        """Total spent so far as a Decimal — use this for math/comparisons."""
        return self._spent

    def would_exceed(self, amount_usdc) -> bool:
        """True if adding this cost would exceed the budget. The recommended
        way to ask "does this fit?" — accepts a str, float, or Decimal."""
        return (self._spent + Decimal(str(amount_usdc))) > self.max_spend

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
                        amount_raw = int(a.get("amount", 0))
                        price_usd = amount_raw / 1_000_000   # USDC has 6 decimals
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
        """
        import time as _time

        # ── Policy checks (reuse same guards as call()) ───────────────────────
        if self._rate_limit is not None:
            now = _time.monotonic()
            self._rate_window = [t for t in self._rate_window if now - t < 60.0]
            if len(self._rate_window) >= self._rate_limit:
                raise BudgetExceeded(
                    f"Rate limit exceeded: max {self._rate_limit} calls/min"
                )
            self._rate_window.append(now)

        with httpx.Client(timeout=60.0) as client:
            # ── First request — probe for 402 ─────────────────────────────────
            # Default POST (AgentPay's own tools); GET-only servers (e.g. CMC's
            # DEX endpoints) answer 405 → re-probe with GET.
            logger.info(f"→ x402 external call: {url}")
            try:
                resp = client.post(url, json=params)
                if resp.status_code == 405:
                    resp = client.get(url)
            except Exception as e:
                raise Exception(f"External x402 call failed: {e}")

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code != 402:
                raise Exception(
                    f"Expected 200 or 402 from {url}, got {resp.status_code}: {resp.text[:200]}"
                )

            # ── Parse payment options (x402 v2 'accepts[]') ───────────────────
            try:
                data = resp.json()
            except Exception:
                raise Exception(f"Could not parse 402 response from {url}: {resp.text[:200]}")
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
                    raise Exception(
                        f"{url} returned an AgentPay-native 402 (payment_options, not "
                        f"x402-v2 'accepts'). Call AgentPay tools by name — "
                        f"session.call('tool_name') — rather than by URL."
                    )
                raise Exception(f"402 from {url} had no payment requirements in 'accepts'")

            # ── Normalise into payable candidates, tagged by chain ────────────
            def _chain_kind(net) -> str | None:
                n = str(net or "").lower()
                if "eip155" in n or n.startswith("base"):
                    return "base"
                if "stellar" in n:
                    return "stellar"
                return None

            candidates = []
            for a in accepts:
                kind_ = _chain_kind(a.get("network"))
                if kind_ is None:
                    continue
                try:
                    atomic = int(a.get("amount", 0))
                except (ValueError, TypeError):
                    continue
                can = bool(self.wallet.base_address) if kind_ == "base" else True  # any Stellar wallet can pay
                candidates.append({
                    "kind": kind_, "network": a.get("network", ""), "pay_to": a.get("payTo"),
                    "amount_atomic": atomic, "amount_usdc": f"{atomic / 1_000_000:.6f}",
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
                offered = sorted({c["kind"] for c in candidates}) or \
                          sorted({str(a.get("network", "?")) for a in accepts})
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

            # ── Budget check ──────────────────────────────────────────────────
            if self.would_exceed(amount_usdc):
                raise BudgetExceeded(
                    f"Tool costs ${float(amount_usdc):.4f} but only "
                    f"{self.remaining()} remains (budget: {_fmt(self.max_spend)})"
                )

            # ── Pay on the selected network and retry ─────────────────────────
            tx_hash = ""
            if kind == "base":
                # Base: sign EIP-3009 OFF-CHAIN — nothing is broadcast. The
                # resource server's facilitator settles the authorization only
                # if it accepts the request, so a rejected retry moves no funds.
                # (This replaces the old raw ERC-20 transfer + tx_hash proof,
                # which paid on-chain BEFORE the provider accepted and lost USDC
                # against CDP-facilitator tools.)
                logger.info(f"  402 — signing {amount_usdc} USDC auth for {pay_to[:10]}... (Base, off-chain)")
                try:
                    x_payment = self.wallet.build_base_payment_signature(base_accept, resource_for_payment)
                except Exception as e:
                    raise PaymentFailed(f"evm:could not sign x402 payment: {str(e)[:160]}")
                payer_address = self.wallet.base_address

                _headers = {
                    "X-PAYMENT":         x_payment,   # x402 v2 standard header
                    "PAYMENT-SIGNATURE": x_payment,   # alias some gateways use
                    "X-Agent-Address":   payer_address,
                }
                retry = (client.get(url, headers=_headers) if req_method == "GET"
                         else client.post(url, json=params, headers=_headers))
                if retry.status_code != 200:
                    # No broadcast happened — no USDC left the wallet.
                    raise Exception(
                        f"External x402 call rejected (no payment settled): "
                        f"{retry.status_code} {retry.text[:200]}"
                    )
                result = retry.json()
                if isinstance(result, dict):
                    tx_hash = ((result.get("payment") or {}).get("tx_hash")) or ""
            else:
                # Stellar: broadcast the payment, then prove it with the tx_hash.
                logger.info(f"  402 — paying {amount_usdc} USDC to {pay_to[:10]}... (Stellar)")
                payment = self.wallet.pay(
                    destination=pay_to, amount_usdc=amount_usdc, memo="agentpay-x402",
                )
                if not payment["success"]:
                    raise PaymentFailed(payment["reason"])
                tx_hash = payment["tx_hash"]
                payer_address = self.wallet.public_key
                logger.info(f"  ✓ Payment sent | tx: {tx_hash[:16]}...")

                proof_payload = {
                    "x402Version": 2,
                    "scheme":      pay_scheme,
                    "network":     pay_network,
                    "payload":     {"signature": tx_hash, "from": payer_address},
                }
                x_payment = base64.b64encode(json.dumps(proof_payload).encode()).decode()
                _headers = {"X-Payment": x_payment, "X-Agent-Address": payer_address}
                retry = (client.get(url, headers=_headers) if req_method == "GET"
                         else client.post(url, json=params, headers=_headers))
                if retry.status_code != 200:
                    raise Exception(
                        f"External x402 call failed after payment: {retry.status_code} {retry.text[:200]}"
                    )
                result = retry.json()

            # ── Record spend (only reached when the call returned 200) ─────────
            cost = Decimal(amount_usdc)
            self._spent += cost
            self._call_log.append({
                "tool":        url,
                "amount_usdc": amount_usdc,
                "tx_hash":     tx_hash,
                "network":     pay_network,
                "success":     True,
                "external":    True,
            })
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
        or "stellar") picks which to settle on; without it, the Session's
        prefer_chain (or cheapest payable option) is used. The chosen chain is
        recorded on the result (``.network``) and the receipt.

        For external URLs, payment goes directly to the tool provider.
        AgentPay tracks the spend locally and enforces the budget cap.

        - Pre-checks the price against remaining budget.
        - If budget would be exceeded, looks for the next-cheapest tool in the
          same category that fits, and uses it as a fallback.
        - If the tool is unavailable after payment attempt, also falls back.
        - Raises BudgetExceeded if no affordable option exists.
        - Records actual spend from the x402 payment receipt.
        """
        import time as _time
        from agentpay._client import AgentPayClient

        # ── External x402 URL: route directly, skip AgentPay registry ─────────
        if isinstance(tool_name, str) and tool_name.startswith(("http://", "https://")):
            return _wrap_result(self._call_x402_url(tool_name, params or {}, chain=chain))

        # ── Resolve paid-tool chain preference (Base default, Stellar fallback)
        # An explicit chain (per-call chain= or Session prefer_chain=) is a hard
        # requirement; otherwise we default to Base so paid AgentPay settlements
        # flow through the CDP/Mode-A path that keeps the Bazaar listing live.
        _chain_is_explicit = bool(chain or self._prefer_chain)
        _prefer_chain = (chain or self._prefer_chain or DEFAULT_PAID_CHAIN).lower()

        params = params or {}

        # ── Policy: allowed_tools whitelist ───────────────────────────────────
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            raise BudgetExceeded(
                f"Tool '{tool_name}' is not in the session allowlist: {self._allowed_tools}"
            )

        # ── Policy: rate_limit (max calls per minute) ─────────────────────────
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

        # ── Policy: max_per_tool cap ──────────────────────────────────────────
        if tool_name in self._max_per_tool:
            already_spent_on_tool = sum(
                Decimal(e["amount_usdc"])
                for e in self._call_log
                if e["tool"] == tool_name
            )
            if already_spent_on_tool >= self._max_per_tool[tool_name]:
                raise BudgetExceeded(
                    f"Per-tool cap reached for '{tool_name}': "
                    f"spent {_fmt(already_spent_on_tool)} of max {_fmt(self._max_per_tool[tool_name])}"
                )

        # ── Resolve which tool to actually call ───────────────────────────────
        tool_info = self._fetch_tool_info(tool_name)
        if tool_info is None:
            fallback = self._find_fallback(category="data", exclude=tool_name)
            if fallback and not self.would_exceed(fallback["price_usdc"]):
                logger.warning(f"'{tool_name}' not found — falling back to '{fallback['name']}'")
                tool_info = fallback
                tool_name = fallback["name"]
            else:
                raise BudgetExceeded(f"Tool '{tool_name}' not found on gateway")

        price = tool_info["price_usdc"]
        target = tool_name

        if self.would_exceed(price):
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
        client = AgentPayClient(wallet=self.wallet, gateway_url=self.gateway_url)
        try:
            result = client.call_tool(
                target, params,
                prefer_chain=_prefer_chain, chain_is_explicit=_chain_is_explicit,
            )
        except PaymentFailed:
            # On-chain payment itself failed — a fallback tool would fail for the
            # same reason (empty wallet, wrong network, etc.). Surface the clean
            # reason to the caller instead of cascading into more failures.
            raise
        except RefundPending:
            # The agent already paid and the gateway has queued a refund.
            # Falling back to another tool would just spend more USDC for
            # another attempt at the same upstream failure mode. Surface
            # the typed exception so callers can branch on it explicitly
            # (see RefundPending docstring for the usage pattern).
            raise
        except Exception as exc:
            # Tool call itself failed (e.g., tool server down post-payment)
            if target == tool_name:
                category = tool_info.get("category", "data")
                fallback = self._find_fallback(category=category, exclude=target)
                if fallback and not self.would_exceed(fallback["price_usdc"]):
                    logger.warning(f"  '{target}' failed ({exc}) — trying '{fallback['name']}'")
                    client = AgentPayClient(wallet=self.wallet, gateway_url=self.gateway_url)
                    result = client.call_tool(
                        fallback["name"], params,
                        prefer_chain=_prefer_chain, chain_is_explicit=_chain_is_explicit,
                    )
                    target = fallback["name"]
                else:
                    raise
            else:
                raise

        # ── Record actual spend from payment receipt ───────────────────────────
        if client.call_log:
            last = client.call_log[-1]
            cost = Decimal(last.get("amount_usdc", "0"))
            self._spent += cost
            self._tool_cache.setdefault(target, {})["price_usdc"] = str(cost)

            # Settlement chain from the gateway receipt (e.g. 'stellar-mainnet',
            # 'base', or 'free' for $0 tools) — recorded so it shows on the receipt.
            net = ""
            if isinstance(result, dict):
                net = (result.get("payment") or {}).get("network", "") or ""
            entry: dict = {
                "tool": target,
                "amount_usdc": str(cost),
                "tx_hash": last.get("tx_hash", ""),
                "network": net,
                "success": True,
            }
            if target != tool_name:
                entry["fallback_for"] = tool_name
            self._call_log.append(entry)

        return _wrap_result(result)

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
        """
        remaining = self.max_spend - self._spent
        candidates = [
            t for t in self._all_tools()
            if t.get("category") == category
            and t.get("name") != exclude
            and t.get("active", True)
            and Decimal(t.get("price_usdc", "999")) <= remaining
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
