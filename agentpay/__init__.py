"""
agentpay — Pay-per-call crypto data for AI agents.

x402 micropayments on Stellar or Base. No API keys. No subscriptions.
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

Gateway URLs:
    Mainnet: https://gateway-production-2cc2.up.railway.app
    Testnet: https://gateway-testnet-production.up.railway.app
"""

from agentpay.client import (
    AgentWallet,
    Session,
    BudgetExceeded,
    faucet_wallet,
    TESTNET_GATEWAY,
    MAINNET_GATEWAY,
)

__version__ = "0.1.1"
__all__ = [
    "AgentWallet",
    "Session",
    "BudgetExceeded",
    "faucet_wallet",
    "TESTNET_GATEWAY",
    "MAINNET_GATEWAY",
]
