"""
client.py — Public-facing wrappers and helpers for the agentpay pip package.
"""

import httpx
from agentpay._wallet import AgentWallet, Session as _Session, BudgetExceeded

TESTNET_GATEWAY  = "https://gateway-testnet-production.up.railway.app"
MAINNET_GATEWAY  = "https://gateway-production-2cc2.up.railway.app"
FAUCET_URL       = f"{TESTNET_GATEWAY}/faucet"


def faucet_wallet() -> AgentWallet:
    """
    Get a testnet wallet pre-loaded with 5 USDC — no setup required.

    Calls the AgentPay faucet, receives a fresh Stellar keypair with
    USDC already deposited. Use this to try AgentPay in seconds.

    Returns:
        AgentWallet ready to use on testnet.

    Example:
        from agentpay import faucet_wallet, Session

        wallet = faucet_wallet()
        with Session(wallet, testnet=True) as s:
            r = s.call("token_price", {"symbol": "ETH"})
            print(r["result"]["price_usd"])
    """
    resp = httpx.get(FAUCET_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    secret = data.get("secret_key") or data.get("secret")
    if not secret:
        raise ValueError(f"Faucet returned unexpected response: {data}")

    balance = data.get("usdc_balance", data.get("balance", "?"))
    print(f"✓ Testnet wallet funded: {balance} USDC")
    print(f"  Public key: {data.get('public_key', '')}")

    return AgentWallet(secret_key=secret, network="testnet")


class Session(_Session):
    """
    Budget-aware session. Wraps _wallet.Session with a cleaner constructor.

    Usage:
        # Testnet shorthand
        with Session(wallet, testnet=True) as s:
            ...

        # Mainnet with budget
        with Session(wallet, max_spend="0.10") as s:
            ...
    """
    def __init__(self, wallet: AgentWallet, max_spend: str = "0.10",
                 testnet: bool = False, gateway_url: str = None):
        if gateway_url is None:
            gateway_url = TESTNET_GATEWAY if testnet else MAINNET_GATEWAY
        super().__init__(wallet=wallet, gateway_url=gateway_url, max_spend=max_spend)


__all__ = ["AgentWallet", "Session", "BudgetExceeded", "faucet_wallet",
           "TESTNET_GATEWAY", "MAINNET_GATEWAY"]
