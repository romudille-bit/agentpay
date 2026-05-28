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

    # Coinbase CDP API credentials — required for authenticated x402 facilitator
    # calls (POST /settle). The CDP Facilitator returns 401 without these.
    # Bazaar auto-indexing only works when settlement flows through CDP.
    #
    # CDP_KEY_NAME:   key name from portal.cdp.coinbase.com
    #                 e.g. "organizations/abc.../apiKeys/xyz..."
    # CDP_KEY_SECRET: EC private key in PEM format. In Railway, store with
    #                 literal \n characters — the gateway restores them on load.
    #                 e.g. "-----BEGIN EC PRIVATE KEY-----\nMHQC...\n-----END EC PRIVATE KEY-----\n"
    CDP_KEY_NAME:   str = ""
    CDP_KEY_SECRET: str = ""

    # PR #12: Async on-chain refund worker. When False (default),
    # tool-failure rows still get state='refund_pending' and the response
    # body still includes payment_status — but the background worker
    # does NOT attempt any on-chain refund. This dark-launch mode is the
    # default so the state tracking can soak in production without
    # committing to actual refund spend.
    #
    # Flip to True via Railway env var (REFUND_ENABLED=true) after a
    # few days of soak. The worker then runs every 60s, attempts each
    # refund on Stellar, retries up to 5 times, transitions to
    # refund_done or refund_failed. Base refunds NOT supported yet
    # (no outgoing Base tx machinery) — Base-paid tools that fail
    # short-circuit to refund_failed with reason='base_refund_not_implemented'.
    REFUND_ENABLED: bool = False

    class Config:
        env_file = "../.env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


# Public-facing gateway URL used in discovery responses, faucet snippets,
# 402 challenge instructions, and the keepalive ping. The AGENTPAY_GATEWAY_URL
# env var (exposed via Settings) overrides the hardcoded default — so swapping
# domains in production is an env var change, not a code redeploy.
GATEWAY_URL = settings.AGENTPAY_GATEWAY_URL or "https://agentpay.tools"


def stellar_caip2() -> str:
    """Return the CAIP-2 network ID for the configured Stellar network.

    The Stellar Foundation's x402 reference and the broader CAIP-2 standard
    expect ``stellar:pubnet`` (NOT ``stellar:mainnet``) for the production
    network. AgentPay's internal env values use the older ``mainnet`` /
    ``testnet`` shorthand, and the in-memory replay tables key on the legacy
    ``stellar-{network}`` label for backward compatibility — but anything we
    *publish* outward (discovery manifests, x402 response advertisements)
    should be CAIP-2.

    Mapping::

        STELLAR_NETWORK="mainnet"  →  "stellar:pubnet"
        STELLAR_NETWORK="testnet"  →  "stellar:testnet"
    """
    return "stellar:pubnet" if settings.STELLAR_NETWORK == "mainnet" else "stellar:testnet"
