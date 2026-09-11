"""
agentpay — Pay-per-call crypto data for AI agents.

x402 micropayments on Base, Stellar, or Stacks (sBTC). No API keys. No subscriptions.
14 live tools: token prices, funding rates, open interest, whale activity,
orderbook depth, DeFi TVL, gas tracker, Fear & Greed, and more.

Quickstart (testnet — free, no wallet needed):
----------------------------------------------
    pip install agentpay-x402

    from agentpay import faucet_wallet, Session

    wallet = faucet_wallet()          # instant testnet wallet with 0.05 USDC
    with Session(wallet, testnet=True) as s:
        r = s.call("token_price", {"symbol": "ETH"})
        print(r["result"]["price_usd"])   # e.g. 1812.34

Quickstart (mainnet):
---------------------
    from agentpay import AgentWallet, Session

    wallet = AgentWallet(secret_key="S...", network="mainnet")
    with Session(wallet, max_spend="0.10") as s:
        r = s.call("funding_rates", {"asset": "ETH"})
        print(r["result"]["rates"])

Pay in sBTC on Stacks (sign-don't-broadcast; the gateway broadcasts):
---------------------------------------------------------------------
    from agentpay import quickstart

    s = quickstart(stacks_key="<64-hex>", prefer_chain="stacks", max_spend="0.05")
    print(s.call("pre_trade_check", {"symbol": "BTC", "size_usd": 25000}).data)

Gateway URLs:
    Mainnet: https://agentpay.tools
    Testnet: https://gateway-testnet-production.up.railway.app
"""

from agentpay.client import (
    AgentWallet,
    Session,
    ToolResult,
    BudgetExceeded,
    ToolNotFound,
    PaymentFailed,
    UnsupportedChainPayment,
    PrePaymentError,
    RefundPending,
    SettlementUncertain,
    faucet_wallet,
    quickstart,
    TESTNET_GATEWAY,
    MAINNET_GATEWAY,
)
from agentpay.budget_policy import budget_policy, BudgetDecision

# Kept in lockstep with pyproject.toml [project].version — enforced by
# tests/test_agentpay_sdk.py::test_version_matches_pyproject. Bump both, or
# the pre-publish test fails.
__version__ = "0.5.0"
__all__ = [
    "AgentWallet",
    "Session",
    "ToolResult",
    "BudgetExceeded",
    "ToolNotFound",
    "PaymentFailed",
    "UnsupportedChainPayment",
    "PrePaymentError",
    "RefundPending",
    "SettlementUncertain",
    "faucet_wallet",
    "quickstart",
    "TESTNET_GATEWAY",
    "MAINNET_GATEWAY",
    "budget_policy",
    "BudgetDecision",
]
