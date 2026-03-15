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

    class Config:
        env_file = "../.env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
