"""
setup_wallet.py — One-time Stellar testnet wallet setup for AgentPay.

Run this ONCE to:
1. Create a gateway wallet (receives all payments)
2. Create a test agent wallet (makes payments)
3. Fund both from Stellar testnet faucet
4. Print .env values to copy

Usage:
    python setup_wallet.py
"""

import httpx
import json
from stellar_sdk import Keypair, Server, Network, Asset, TransactionBuilder


HORIZON_URL = "https://horizon-testnet.stellar.org"
FRIENDBOT_URL = "https://friendbot.stellar.org"
USDC_ISSUER_TESTNET = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"


def create_and_fund_wallet(name: str) -> dict:
    """Create a new Stellar keypair and fund it via friendbot."""
    keypair = Keypair.random()
    print(f"\n[{name}] Created keypair")
    print(f"  Public:  {keypair.public_key}")
    print(f"  Secret:  {keypair.secret}")

    print(f"  Funding from testnet faucet...")
    try:
        resp = httpx.get(FRIENDBOT_URL, params={"addr": keypair.public_key}, timeout=15.0)
        if resp.status_code == 200:
            print(f"  Funded with 10,000 XLM ✓")
        else:
            print(f"  Faucet skipped (will fund manually)")
    except Exception:
        print(f"  Faucet timed out — will fund manually via browser")

    return {"public": keypair.public_key, "secret": keypair.secret}


def add_usdc_trustline(secret_key: str, name: str):
    """Add USDC trustline so the wallet can receive USDC."""
    server = Server(HORIZON_URL)
    keypair = Keypair.from_secret(secret_key)
    usdc = Asset("USDC", USDC_ISSUER_TESTNET)

    print(f"\n[{name}] Adding USDC trustline...")
    try:
        account = server.load_account(keypair.public_key)
        tx = (
            TransactionBuilder(
                source_account=account,
                network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
                base_fee=100,
            )
            .append_change_trust_op(asset=usdc)
            .set_timeout(30)
            .build()
        )
        tx.sign(keypair)
        server.submit_transaction(tx)
        print(f"  USDC trustline added")
    except Exception as e:
        print(f"  Error adding trustline: {e}")


def get_test_usdc(recipient_public: str, issuer_secret: str = None):
    """
    Note: On testnet you need USDC from the testnet USDC issuer.
    In practice, use the Circle testnet faucet or swap testnet XLM for USDC
    via the Stellar DEX.
    
    For quick testing, you can also use XLM directly and modify the 
    payment handler to accept XLM instead of USDC.
    """
    print(f"\n[Test USDC] To get testnet USDC:")
    print(f"  1. Go to https://laboratory.stellar.org/#txbuilder")
    print(f"  2. Or use the Circle testnet faucet")
    print(f"  3. Or swap testnet XLM → USDC on the Stellar testnet DEX")
    print(f"  Recipient: {recipient_public}")


def main():
    print("=" * 60)
    print("AgentPay — Stellar Testnet Wallet Setup")
    print("=" * 60)

    # 1. Create gateway wallet
    gateway = create_and_fund_wallet("GATEWAY")

    # 2. Create agent test wallet
    agent = create_and_fund_wallet("TEST AGENT")

    # 3. Add USDC trustlines
    add_usdc_trustline(gateway["secret"], "GATEWAY")
    add_usdc_trustline(agent["secret"], "TEST AGENT")

    # 4. USDC instructions
    get_test_usdc(agent["public"])

    # 5. Print .env values
    print("\n" + "=" * 60)
    print("Copy these values into your .env file:")
    print("=" * 60)
    print(f"""
STELLAR_NETWORK=testnet
GATEWAY_SECRET_KEY={gateway['secret']}
GATEWAY_PUBLIC_KEY={gateway['public']}
GATEWAY_FEE_PERCENT=0.15

# Test agent wallet (for running agent.py)
TEST_AGENT_SECRET_KEY={agent['secret']}
TEST_AGENT_PUBLIC_KEY={agent['public']}
""")

    # 6. Save to file for convenience
    with open("wallets.json", "w") as f:
        json.dump({"gateway": gateway, "agent": agent}, f, indent=2)
    print("Also saved to wallets.json (keep this secret, never commit to git!)")
    print("\nNext step: run `cd gateway && uvicorn main:app --reload`")


if __name__ == "__main__":
    main()
