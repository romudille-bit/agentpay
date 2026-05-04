"""
config.py — Environment configuration for AgentPay gateway.
"""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Stellar
    STELLAR_NETWORK: str = "testnet"
    GATEWAY_SECRET_KEY: str = ""
    GATEWAY_PUBLIC_KEY: str = ""
    GATEWAY_FEE_PERCENT: float = 0.15

    # USDC issuers
    USDC_ISSUER_TESTNET: str = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
    USDC_ISSUER_MAINNET: str = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"

    # Server
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    # External APIs
    COINGECKO_API_URL: str = "https://api.coingecko.com/api/v3"
    ETHERSCAN_API_KEY: str = ""

    # Database
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

    # OpenAI (for agent)
    OPENAI_API_KEY: str = ""

    # Dune Analytics
    DUNE_API_KEY: str = ""

    # Public gateway URL (used in faucet snippets, discovery endpoints)
    AGENTPAY_GATEWAY_URL: str = ""

    # OpenZeppelin x402 Facilitator (covers XLM gas fees — agents only need USDC)
    # Disabled by default since early 2026 — the OZ x402 channel returns 401
    # in production for all requests until credentials are wired up. With #18
    # the gateway no longer wastes a 15-second POST → 401 round-trip on every
    # verification and goes straight to Horizon. Set to True once OZ auth is
    # configured (or if testing the facilitator flow specifically).
    STELLAR_FACILITATOR_URL: str = "https://channels.openzeppelin.com/x402"
    STELLAR_FACILITATOR_ENABLED: bool = False

    # Base / EVM payment option (via Coinbase CDP x402 facilitator)
    # Default network is mainnet ("base") to match BASE_RPC_URL below — using
    # "base-sepolia" with a mainnet RPC produced silent verification failures
    # because the gateway looked for a sepolia USDC transfer event on a mainnet
    # receipt. Override BASE_NETWORK + BASE_RPC_URL together for testnet.
    BASE_GATEWAY_ADDRESS: str = ""           # 0x... recipient on Base
    BASE_NETWORK: str = "base"               # "base" (mainnet) or "base-sepolia"
    BASE_RPC_URL: str = "https://mainnet.base.org"

    # 402index.io domain verification — public sha256 hash served at
    # /.well-known/402index-verify.txt. Leave blank to serve 404.
    INDEX402_VERIFY_HASH: str = ""

    class Config:
        env_file = "../.env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


# Public-facing gateway URL used in discovery responses, faucet snippets,
# 402 challenge instructions, and the keepalive ping. Settings exposes
# AGENTPAY_GATEWAY_URL as an override; this constant is the stable fallback
# pointing at the production deploy.
GATEWAY_URL = "https://gateway-production-2cc2.up.railway.app"
