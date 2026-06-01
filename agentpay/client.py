"""
client.py — Public-facing wrappers and helpers for the agentpay pip package.
"""

import httpx
from agentpay._wallet import (
    AgentWallet,
    Session as _Session,
    BudgetExceeded,
    PaymentFailed,
    RefundPending,
    ToolResult,
)

TESTNET_GATEWAY  = "https://gateway-testnet-production.up.railway.app"
MAINNET_GATEWAY  = "https://agentpay.tools"
FAUCET_URL       = f"{TESTNET_GATEWAY}/faucet"


def faucet_wallet() -> AgentWallet:
    """
    Get a testnet wallet pre-loaded with 0.05 USDC — no setup required.

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
    resp = httpx.get(FAUCET_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    secret = data.get("secret_key") or data.get("secret")
    if not secret:
        raise ValueError(f"Faucet returned unexpected response: {data}")

    balance = data.get("usdc_balance", data.get("balance", "?"))
    print(f"✓ Testnet wallet funded: {balance} USDC")
    print(f"  Public key: {data.get('public_key', '')}")

    return AgentWallet(secret_key=secret, network="testnet")


def quickstart(
    max_spend: str = "0.10",
    *,
    secret_key: str = None,
    base_key: str = None,
    network: str = "mainnet",
    testnet: bool = False,
    gateway_url: str = None,
    label: str = "quickstart",
    prefer_chain: str = None,
    quiet: bool = False,
) -> "Session":
    """
    Zero-setup entry point — your first call needs no keys, no funding, no human.

    Registers an agent (mints a fresh wallet + session token via the free
    POST /v1/agent/register), and returns a ready, budget-capped Session. The
    17 free tools work immediately on the minted wallet (free tools never settle
    on-chain, so no USDC is required).

        from agentpay import quickstart

        s = quickstart()
        print(s.call("token_price", {"symbol": "ETH"})["result"]["price_usd"])
        print(s.spending_summary())          # receipt: every call, cost, tx

    Bring your own funded wallet instead of minting one (to pay for tools):

        s = quickstart(secret_key="S...", base_key="0x...", max_spend="0.50")

    The returned Session also carries:
        s.session_token       — server session id
        s.free_tools          — list of tools callable for $0
        s.wallet_public_key   — the minted (or provided) wallet address

    Args:
        max_spend:   hard budget cap for the session (string USDC, e.g. "0.10").
        secret_key:  bring-your-own Stellar secret; if given, skips registration.
        base_key:    optional Base/EVM key (0x...) to pay tools that settle on Base.
        network:     "mainnet" (default) or "testnet".
        testnet:     shorthand for network="testnet" + testnet gateway.
        gateway_url: override the gateway (defaults to mainnet/testnet URL).
        label:       human/agent label recorded with the registration.
        quiet:       suppress the friendly one-line startup print.

    Returns:
        Session — ready to call free tools immediately.
    """
    gw  = gateway_url or (TESTNET_GATEWAY if testnet else MAINNET_GATEWAY)
    net = "testnet" if testnet else network

    # ── Bring-your-own-wallet: skip registration ──────────────────────────────
    if secret_key:
        try:
            wallet = AgentWallet(secret_key=secret_key, network=net, base_key=base_key)
        except TypeError:                       # older build without base_key kwarg
            wallet = AgentWallet(secret_key=secret_key, network=net)
        s = Session(wallet, max_spend=max_spend, gateway_url=gw, prefer_chain=prefer_chain)
        s.session_token, s.free_tools, s.wallet_public_key = None, [], wallet.public_key
        if not quiet:
            print(f"✓ AgentPay ready — your wallet {wallet.public_key[:10]}…, budget ${max_spend}.")
        return s

    # ── Mint a fresh wallet via the free, zero-human register endpoint ─────────
    try:
        resp = httpx.post(f"{gw}/v1/agent/register",
                          json={"label": label, "network": "stellar"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise RuntimeError(
            f"quickstart() could not register an agent at {gw}/v1/agent/register "
            f"({e}). For a funded testnet wallet instead, use faucet_wallet()."
        )

    w = data.get("wallet", {}) or {}
    minted_secret = w.get("secret_key")
    if not minted_secret:
        raise ValueError(f"register returned no wallet secret: {data}")

    try:
        wallet = AgentWallet(secret_key=minted_secret, network=net, base_key=base_key)
    except TypeError:
        wallet = AgentWallet(secret_key=minted_secret, network=net)

    s = Session(wallet, max_spend=max_spend, gateway_url=gw, prefer_chain=prefer_chain)
    s.session_token     = data.get("session_token")
    s.free_tools        = data.get("free_tools", [])
    s.wallet_public_key = w.get("public_key")
    if not quiet:
        print(f"✓ AgentPay ready — minted wallet {s.wallet_public_key[:10] if s.wallet_public_key else ''}…, "
              f"{len(s.free_tools)} free tools, budget ${max_spend}.")
        print("  Free tools need no funding. Fund this wallet (USDC) only to pay for tools.")
    return s


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
                 testnet: bool = False, gateway_url: str = None,
                 prefer_chain: str = None):
        if gateway_url is None:
            gateway_url = TESTNET_GATEWAY if testnet else MAINNET_GATEWAY
        super().__init__(wallet=wallet, gateway_url=gateway_url, max_spend=max_spend,
                         prefer_chain=prefer_chain)


__all__ = ["AgentWallet", "Session", "ToolResult", "BudgetExceeded", "PaymentFailed",
           "RefundPending", "faucet_wallet", "quickstart",
           "TESTNET_GATEWAY", "MAINNET_GATEWAY"]
