"""
agentpay — SDK for paying AI agent tools via x402 on Stellar.

Usage:
    import agentpay

    # One-time setup
    agentpay.configure(secret_key="S...", network="testnet")

    # Simple call (auto-pays)
    result = agentpay.call("token_price", {"symbol": "ETH"})

    # Budget-managed session
    with agentpay.Session(max_spend="0.10") as session:
        price   = session.call("token_price",    {"symbol": "ETH"})
        liq     = session.call("dex_liquidity",  {"token_a": "ETH"})
        whales  = session.call("whale_activity", {"token": "ETH"})
        print(session.summary())
"""

import os
from agent.wallet import AgentWallet, BudgetSession

# Global wallet instance (set via configure())
_wallet: AgentWallet = None
_gateway_url: str = "http://localhost:8000"


def configure(
    secret_key: str = None,
    network: str = "testnet",
    gateway_url: str = "http://localhost:8000",
):
    """
    Configure the AgentPay SDK with your Stellar wallet.

    Args:
        secret_key: Stellar secret key (S...). Falls back to
                    TEST_AGENT_SECRET_KEY env var if not provided.
        network:    "testnet" or "mainnet"
        gateway_url: AgentPay gateway URL
    """
    global _wallet, _gateway_url
    key = secret_key or os.getenv("TEST_AGENT_SECRET_KEY", "")
    if not key:
        raise ValueError(
            "No secret key provided. Pass secret_key= or set TEST_AGENT_SECRET_KEY in .env"
        )
    _wallet = AgentWallet(secret_key=key, network=network)
    _gateway_url = gateway_url


def call(tool_name: str, parameters: dict = None, max_spend: str = None) -> dict:
    """
    Call a paid AgentPay tool. Handles 402 payment automatically.

    Args:
        tool_name:  Name of the tool, e.g. "token_price"
        parameters: Tool input parameters
        max_spend:  Optional per-call spend cap in USDC, e.g. "0.005"

    Returns:
        Tool result as a dict

    Example:
        result = agentpay.call("token_price", {"symbol": "ETH"})
    """
    global _wallet, _gateway_url
    if _wallet is None:
        configure()  # auto-configure from env

    from agent.agent import AgentPayClient
    client = AgentPayClient(wallet=_wallet, gateway_url=_gateway_url)
    return client.call_tool(tool_name, parameters or {}, max_spend=max_spend)


def Session(max_spend: str = "0.10") -> "BudgetSession":
    """
    Create a budget-managed session for multi-tool tasks.

    Args:
        max_spend: Maximum total USDC to spend across all calls

    Example:
        with agentpay.Session(max_spend="0.10") as s:
            price = s.call("token_price", {"symbol": "ETH"})
            liq   = s.call("dex_liquidity", {"token_a": "ETH"})
            print(s.summary())
    """
    global _wallet, _gateway_url
    if _wallet is None:
        configure()
    return BudgetSession(wallet=_wallet, gateway_url=_gateway_url, max_spend=max_spend)
