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

    # Keepalive ping target. Empty = localhost (keeps the worker warm without
    # a round-trip through Railway's edge). Set to the public /health URL if
    # edge-traffic-based app sleeping is ever enabled on the service.
    KEEPALIVE_URL: str = ""

    # Per-wallet rate limit on /tools/{name}/call, keyed on the declared
    # X-Agent-Address (IP fallback). Runs alongside the per-IP limit.
    WALLET_RATE_LIMIT: str = "60/minute"

    # OpenZeppelin x402 Facilitator (covers XLM gas fees — agents only need USDC)
    # Disabled by default since early 2026 — the OZ x402 channel returns 401
    # in production for all requests until credentials are wired up. With #18
    # the gateway no longer wastes a 15-second POST → 401 round-trip on every
    # verification and goes straight to Horizon. Set to True once OZ auth is
    # configured (or if testing the facilitator flow specifically).
    STELLAR_FACILITATOR_URL: str = "https://channels.openzeppelin.com/x402"
    STELLAR_FACILITATOR_ENABLED: bool = False

    # ── Stacks / sBTC settlement (AGE-23) ─────────────────────────────────────
    # Inert by default: with STACKS_ENABLED false (or no gateway address) the
    # 402 never offers a stacks option and the settle path 503s.
    STACKS_ENABLED: bool = False
    STACKS_NETWORK: str = "testnet"          # "testnet" | "mainnet"
    STACKS_HIRO_API: str = ""                # default derived from STACKS_NETWORK
    STACKS_FACILITATOR_URL: str = ""         # empty ⇒ direct-broadcast mode only
    STACKS_GATEWAY_ADDRESS: str = ""         # c32 (SP…/ST…); fund STX for fees
    STACKS_SBTC_CONTRACT: str = ""           # default derived from STACKS_NETWORK
    STACKS_SETTLE_TIMEOUT_S: float = 30.0
    STACKS_CONFIRM_POLL_S: float = 3.0
    STACKS_CONFIRM_MAX_POLLS: int = 20
    STACKS_SUGGESTED_FEE_MICROSTX: int = 3000
    # M1 stopgap USD→sats quote rate (e.g. "97000"). AGE-24 replaces this
    # with a live FX source + per-payment rate/quote recording.
    STACKS_FIXED_BTC_USD: str = ""

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

    # Search-engine verification + instant indexing (SEO). All optional;
    # blank = tag/endpoint not emitted. Set via Railway env vars.
    #
    # GOOGLE_SITE_VERIFICATION: content token from Google Search Console's
    #   "HTML tag" method → rendered as <meta name="google-site-verification">
    #   on the landing page.
    # BING_SITE_VERIFICATION: content token from Bing Webmaster Tools
    #   ("HTML Meta Tag" method) → <meta name="msvalidate.01">.
    # INDEXNOW_KEY: any hex key (self-generated, e.g. `openssl rand -hex 16`).
    #   Served at GET /indexnow.txt; submit URLs with
    #   keyLocation=https://agentpay.tools/indexnow.txt for instant Bing/
    #   Seznam/Naver/Yandex indexing.
    GOOGLE_SITE_VERIFICATION: str = ""
    BING_SITE_VERIFICATION: str = ""
    INDEXNOW_KEY: str = ""

    # Coinbase CDP API credentials — required for authenticated x402 facilitator
    # calls (POST /settle). The CDP Facilitator returns 401 without these.
    # Bazaar auto-indexing only works when settlement flows through CDP.
    #
    # CDP_KEY_ID:     Key ID from portal.cdp.coinbase.com → Secret API keys
    #                 (UUID format, e.g. "472e91b4-...")
    # CDP_KEY_SECRET: API key secret from the same portal.
    #                 Ed25519 keys: short base64 string.
    #                 EC keys (cloud.coinbase.com): PEM string with literal \n.
    CDP_KEY_ID:     str = ""
    CDP_KEY_SECRET: str = ""

    # Base gateway wallet PRIVATE key (0x...) — required only for OUTGOING
    # Base transfers (refunds). Receiving/verifying needs just
    # BASE_GATEWAY_ADDRESS. Unset = Base refunds short-circuit to
    # refund_failed ('base_refund_not_implemented'), as before.
    BASE_GATEWAY_SECRET_KEY: str = ""

    # Async on-chain refund worker. When False (default),
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

    # Revenue-split resilience. split_payment() forwards the developer's 85%
    # on every paid call; before this it was fire-and-forget with no retry,
    # so any transient Horizon blip or momentary low-XLM on the gateway
    # silently lost the developer their cut. split_payment now retries up to
    # SPLIT_MAX_RETRIES times with exponential backoff, and on final failure
    # durably stamps the payment_logs row (error_reason='split_failed: ...')
    # so a permanently-failed split is queryable for manual reconciliation
    # instead of vanishing. Set to 0 to disable retries (single attempt).
    SPLIT_MAX_RETRIES: int = 3
    SPLIT_RETRY_BASE_DELAY: float = 0.5   # seconds; doubles each attempt

    # Arbitrum x402 Radar discovery endpoint (GET /discovery/arbitrum). Additive,
    # read-only, public. Kept behind a flag (default on) so it can be disabled
    # without a redeploy if the upstream Bazaar discovery API misbehaves — mirrors
    # the REFUND_ENABLED dark-launch pattern. Set RADAR_ENABLED=false to 404 it.
    RADAR_ENABLED: bool = True

    # Demo mode: when set to a local JSON path, /discovery/arbitrum serves that
    # captured Bazaar payload instead of calling live Bazaar. Used for deterministic
    # demos/recordings (live Bazaar has few/no Arbitrum-stack tools yet). Empty =
    # normal live behavior. Example: RADAR_DEMO_FIXTURE=tests/fixtures/bazaar.json
    RADAR_DEMO_FIXTURE: str = ""

    # Canonical RadarSplit contract per chain (POST /discovery/arbitrum/verify).
    # Empty = verification unavailable for that chain (503). Set after deploy.
    RADAR_CONTRACT_ARBITRUM: str = ""
    RADAR_CONTRACT_ARBITRUM_SEPOLIA: str = ""
    RADAR_CONTRACT_ROBINHOOD: str = ""
    RADAR_RPC_ARBITRUM: str = "https://arb1.arbitrum.io/rpc"
    RADAR_RPC_ARBITRUM_SEPOLIA: str = "https://sepolia-rollup.arbitrum.io/rpc"
    RADAR_RPC_ROBINHOOD: str = ""    # no stable public default yet

    # Public flagship receipt ledger (GET /ledger + GET /v1/ledger.json). Reads
    # payment_logs for the flagship analyst agent's wallets and renders its run
    # history, spend-vs-cap, and on-chain links — the live proof that an agent
    # manages its own budget on AgentPay's rails. Additive, read-only, public,
    # and behind a flag (default on) like RADAR_ENABLED so it can be 404'd without
    # a redeploy. LEDGER_FLAGSHIP_ADDRESSES is a comma-separated allowlist of the
    # agent's wallet addresses (its Base payer + its Stellar free-tier identity);
    # empty = the built-in default pair in routes/ledger.py.
    LEDGER_ENABLED: bool = True
    LEDGER_FLAGSHIP_ADDRESSES: str = ""
    LEDGER_RUN_CAP_USDC: str = "0.25"   # hard per-run cap the flagship runs under

    # Shared secret that gates POST /v1/flagship/run — the flagship agent posts
    # its run summary (plan, regime, verdicts, receipt, note) so /ledger can show
    # the reasoning behind each call. The gateway holds the Supabase creds and
    # does the write; the agent stays a credential-free HTTP customer. Empty =
    # ingest endpoint disabled (404). Must match the agent's FLAGSHIP_INGEST_SECRET.
    FLAGSHIP_INGEST_SECRET: str = ""

    # AGE-59: shared secret that gates POST /tools/register. There is no
    # third-party developer registration flow yet, so the endpoint is
    # OPERATOR-ONLY: empty = registration disabled (404, mirrors the flagship
    # ingest pattern). Set a ≥128-bit random value to enable. When a real
    # dev-onboarding flow ships (AGE-71's Supabase-backed registry), replace
    # with per-developer API keys.
    TOOL_REGISTER_SECRET: str = ""

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


def offered_pending_network() -> str:
    """Network label for a pre-402 pending payment_logs row.

    A pending row is a challenge that has been ISSUED but not yet settled, so
    we don't yet know which chain (if any) the client will pay on. Label it
    with the chain the 402 LEADS with — Base when a Base gateway wallet is
    configured (the canonical paid chain), else Stellar. Without this, every
    abandoned-at-402 row defaults to ``stellar-mainnet`` and analytics can't
    tell Base-intent from Stellar-intent abandoners (the "it's all Stellar"
    reporting artifact). The terminal payment_done PATCH overwrites this with
    the chain that actually settled, so completed rows stay accurate.

    Labels are chosen to normalize cleanly (see ledger._norm_network): Base
    mainnet → ``base-mainnet`` (not ``base-base``); Base testnet keeps its
    ``base-sepolia`` value; Stellar → ``stellar-{network}``.
    """
    if settings.BASE_GATEWAY_ADDRESS:
        return "base-mainnet" if settings.BASE_NETWORK == "base" else settings.BASE_NETWORK
    return f"stellar-{settings.STELLAR_NETWORK}"
