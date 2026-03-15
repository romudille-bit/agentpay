"""
wallet.py — Agent-side Stellar wallet + budget session management.

Two main classes:
  AgentWallet  — Stellar wallet that sends USDC payments
  Session      — Budget-aware session with fallback routing
"""

import httpx
import logging
from decimal import Decimal

from stellar_sdk import (
    Keypair, Server, Network, Asset,
    TransactionBuilder
)

logger = logging.getLogger(__name__)

HORIZON_TESTNET = "https://horizon-testnet.stellar.org"
HORIZON_MAINNET = "https://horizon.stellar.org"
USDC_ISSUER_TESTNET = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
USDC_ISSUER_MAINNET = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"


# ── Exceptions ────────────────────────────────────────────────────────────────

class BudgetExceeded(Exception):
    """Raised when a tool call would exceed the session budget."""
    pass


# ── Stellar Wallet ────────────────────────────────────────────────────────────

class AgentWallet:
    """
    Stellar wallet for an AI agent.
    Manages USDC payments to AgentPay gateway in response to 402 challenges.
    """

    def __init__(self, secret_key: str, network: str = "testnet"):
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

    @property
    def public_key(self) -> str:
        return self.keypair.public_key

    @property
    def total_spent_usdc(self) -> str:
        return str(self._total_spent)

    def get_usdc_balance(self) -> str:
        """Return current USDC balance."""
        try:
            account = self.server.load_account(self.public_key)
            for b in account.raw_data.get("balances", []):
                if b.get("asset_code") == "USDC":
                    return b.get("balance", "0")
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
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
            logger.error(f"Payment failed: {e}")
            return {"success": False, "reason": str(e)}

    def would_exceed_budget(self, amount_usdc: str, max_budget: str) -> bool:
        """Return True if paying this amount would exceed the budget."""
        return (self._total_spent + Decimal(amount_usdc)) > Decimal(max_budget)


# ── Budget-Aware Session ──────────────────────────────────────────────────────

def _fmt(amount) -> str:
    """Format a Decimal/str/float as '$0.0030' with clean trailing-zero stripping."""
    s = f"{Decimal(str(amount)):.7f}".rstrip("0").rstrip(".")
    return f"${s}"


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

    def __init__(self, wallet: AgentWallet, gateway_url: str, max_spend: str = "0.10"):
        self.wallet = wallet
        self.gateway_url = gateway_url.rstrip("/")
        self.max_spend = Decimal(max_spend)
        self._spent = Decimal("0")
        self._call_log: list[dict] = []
        self._tool_cache: dict[str, dict] = {}   # tool_name → full tool metadata
        self._all_tools_cache: list[dict] | None = None

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
        """Remaining budget as formatted string, e.g. '$0.097'."""
        rem = max(self.max_spend - self._spent, Decimal("0"))
        return _fmt(rem)

    def spent(self) -> str:
        """Total spent so far as formatted string."""
        return _fmt(self._spent)

    def would_exceed(self, amount_usdc: str) -> bool:
        """True if adding this cost would exceed the budget."""
        return (self._spent + Decimal(amount_usdc)) > self.max_spend

    def call(self, tool_name: str, params: dict = None) -> dict:
        """
        Call a paid tool within budget.

        - Pre-checks the price against remaining budget.
        - If budget would be exceeded, looks for the next-cheapest tool in the
          same category that fits, and uses it as a fallback.
        - If the tool is unavailable after payment attempt, also falls back.
        - Raises BudgetExceeded if no affordable option exists.
        - Records actual spend from the x402 payment receipt.
        """
        from agent.agent import AgentPayClient

        params = params or {}

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
            result = client.call_tool(target, params)
        except Exception as exc:
            # Tool call itself failed (e.g., tool server down post-payment)
            if target == tool_name:
                category = tool_info.get("category", "data")
                fallback = self._find_fallback(category=category, exclude=target)
                if fallback and not self.would_exceed(fallback["price_usdc"]):
                    logger.warning(f"  '{target}' failed ({exc}) — trying '{fallback['name']}'")
                    client = AgentPayClient(wallet=self.wallet, gateway_url=self.gateway_url)
                    result = client.call_tool(fallback["name"], params)
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

            entry: dict = {
                "tool": target,
                "amount_usdc": str(cost),
                "tx_hash": last.get("tx_hash", ""),
                "success": True,
            }
            if target != tool_name:
                entry["fallback_for"] = tool_name
            self._call_log.append(entry)

        return result

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
            label = entry["tool"]
            if "fallback_for" in entry:
                label += f"  (fallback for {entry['fallback_for']})"
            lines.append(
                f"    {label:<30} {_fmt(entry['amount_usdc'])}"
                + (f"  |  {tx}..." if tx else "")
            )
        lines.append("─" * width)
        return "\n".join(lines)


# Backwards-compatible alias
BudgetSession = Session
