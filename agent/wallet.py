"""
wallet.py — Agent-side Stellar wallet + budget session management.

Two main classes:
  AgentWallet    — Stellar wallet that sends USDC payments
  BudgetSession  — Budget-managed multi-tool session
"""

from stellar_sdk import (
    Keypair, Server, Network, Asset,
    TransactionBuilder
)
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

HORIZON_TESTNET = "https://horizon-testnet.stellar.org"
HORIZON_MAINNET = "https://horizon.stellar.org"
USDC_ISSUER_TESTNET = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
USDC_ISSUER_MAINNET = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"


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


class BudgetExceeded(Exception):
    """Raised when a tool call would exceed the session budget."""
    pass


class BudgetSession:
    """
    Budget-managed session for multi-tool agent tasks.

    Tracks spend across multiple tool calls and enforces a hard cap.
    Use as a context manager for automatic summary on exit.

    Example:
        with BudgetSession(wallet, gateway_url, max_spend="0.10") as session:
            price = session.call("token_price", {"symbol": "ETH"})
            liq   = session.call("dex_liquidity", {"token_a": "ETH"})
            print(session.summary())
    """

    def __init__(self, wallet: AgentWallet, gateway_url: str, max_spend: str = "0.10"):
        self.wallet = wallet
        self.gateway_url = gateway_url
        self.max_spend = Decimal(max_spend)
        self._spent = Decimal("0")
        self._call_log: list[dict] = []
        self._price_cache: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._call_log:
            print(self._format_summary())

    # ── Core ─────────────────────────────────────────────────────────────────

    def call(self, tool_name: str, parameters: dict = None) -> dict:
        """
        Call a paid tool within budget. Raises BudgetExceeded if over cap.
        """
        from agent.agent import AgentPayClient

        # Pre-flight budget check using cached/fetched price
        estimated = self._get_price(tool_name)
        if estimated and (self._spent + Decimal(estimated)) > self.max_spend:
            raise BudgetExceeded(
                f"'{tool_name}' costs ~{estimated} USDC but only "
                f"{self.remaining()} USDC remains (budget: {self.max_spend})"
            )

        client = AgentPayClient(wallet=self.wallet, gateway_url=self.gateway_url)
        result = client.call_tool(tool_name, parameters or {})

        # Record actual spend
        if client.call_log:
            last = client.call_log[-1]
            cost = Decimal(last.get("amount_usdc", "0"))
            self._spent += cost
            self._price_cache[tool_name] = str(cost)
            self._call_log.append({
                "tool": tool_name,
                "amount_usdc": str(cost),
                "tx_hash": last.get("tx_hash", ""),
                "success": True,
            })

        return result

    # ── Budget introspection ──────────────────────────────────────────────────

    def estimate(self, tool_name: str) -> str:
        """Price of a tool in USDC (fetched from gateway)."""
        return self._get_price(tool_name) or "unknown"

    def spent(self) -> str:
        return str(self._spent)

    def remaining(self) -> str:
        return str(max(self.max_spend - self._spent, Decimal("0")))

    def would_exceed(self, amount_usdc: str) -> bool:
        return (self._spent + Decimal(amount_usdc)) > self.max_spend

    def summary(self) -> dict:
        return {
            "calls": len(self._call_log),
            "spent_usdc": self.spent(),
            "remaining_usdc": self.remaining(),
            "max_spend_usdc": str(self.max_spend),
            "breakdown": self._call_log,
        }

    def print_summary(self):
        print(self._format_summary())

    def _format_summary(self) -> str:
        lines = [
            "",
            "─" * 52,
            "  AgentPay Session Summary",
            "─" * 52,
            f"  Calls made:   {len(self._call_log)}",
            f"  Total spent:  {self.spent()} USDC",
            f"  Remaining:    {self.remaining()} USDC  (budget: {self.max_spend})",
            "",
            "  Breakdown:",
        ]
        for entry in self._call_log:
            tx = (entry.get("tx_hash") or "")[:14]
            lines.append(
                f"    {entry['tool']:<22} {entry['amount_usdc']} USDC"
                + (f"  |  tx: {tx}..." if tx else "")
            )
        lines.append("─" * 52)
        return "\n".join(lines)

    def _get_price(self, tool_name: str) -> str | None:
        if tool_name in self._price_cache:
            return self._price_cache[tool_name]
        try:
            import httpx
            resp = httpx.get(f"{self.gateway_url}/tools/{tool_name}", timeout=5.0)
            if resp.status_code == 200:
                price = resp.json().get("price_usdc")
                if price:
                    self._price_cache[tool_name] = price
                    return price
        except Exception:
            pass
        return None
