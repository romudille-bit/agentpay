# AgentPay — ![tests](https://github.com/romudille-bit/agentpay/actions/workflows/test.yml/badge.svg) Ditch those API keys.

Pay per call. No subscriptions. No human in the loop.

AgentPay is an x402 payment gateway for AI agents. Access 14 crypto data tools using USDC on Stellar or Base — agents discover, pay, and get data back autonomously, within a hard budget cap.

→ **14 live tools**: token prices, open interest, orderbook depth, whale activity, funding rates, gas tracker, DeFi TVL, Fear & Greed, yield scanner, token security, Dune queries and more
→ **Budget-aware Session**: agents estimate costs, track spend, never exceed budget
→ **x402 protocol**: works with any x402-compatible agent
→ **Stellar + Base**: pay with USDC on either network — Stellar (5s, ~$0.00001 fee) or Base mainnet (2s, ~$0.0001 fee)

**Live gateway (mainnet)**: `https://gateway-production-2cc2.up.railway.app`

---

## Install

```bash
pip install agentpay-x402
```

## Quickstart — try it free in 30 seconds (testnet)

```python
from agentpay import faucet_wallet, Session

wallet = faucet_wallet()          # instant testnet wallet, 0.05 USDC — no setup
with Session(wallet, testnet=True) as s:
    r = s.call("token_price", {"symbol": "ETH"})
    print(r["result"]["price_usd"])
```

That's it. No API keys, no accounts, no config.

---

## Quickstart — mainnet

### Step 1: Get a wallet with USDC

**Option A — Stellar mainnet (recommended)**

Send USDC to a Stellar wallet via [Coinbase](https://coinbase.com), [Lobstr](https://lobstr.co), or any Stellar DEX. Then use your Stellar secret key (`S...`) directly.

USDC issuer on Stellar mainnet: `GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN`
Gateway: `https://gateway-production-2cc2.up.railway.app` — use `network="mainnet"`

**Option B — Base mainnet (alternative)**

Send USDC to an EVM wallet on Base. Use your EVM private key with `network="base"`.

USDC contract on Base: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
Gateway: `https://gateway-production-2cc2.up.railway.app` — use `network="base"`

**Option C — Testnet (free, no wallet needed)**

Use `faucet_wallet()` from the SDK — one line, instant wallet with 0.05 USDC:

```python
from agentpay import faucet_wallet
wallet = faucet_wallet()   # prints public key + balance
```

Or via curl:

```bash
curl https://gateway-testnet-production.up.railway.app/faucet
```

---

### Step 2: Create a Session with a budget

```python
from agentpay import AgentWallet, Session, BudgetExceeded

# Option A — Stellar mainnet
wallet = AgentWallet(secret_key="S...", network="mainnet")

# Option B — Base mainnet
# wallet = AgentWallet(secret_key="0x...", network="base")

# Option C — Testnet (faucet wallet)
# wallet = AgentWallet(secret_key="S...", network="testnet")

with Session(wallet, max_spend="0.05") as session:
    print(f"Balance:  {wallet.get_usdc_balance()} USDC")
    print(f"Budget:   {session.remaining()} remaining")
```

The `Session` enforces a hard USDC cap across all calls. It raises `BudgetExceeded` before any payment goes out if a tool would push you over, and automatically falls back to the next-cheapest tool in the same category when the preferred one is too expensive.

---

### Step 3: Call tools — payment is automatic

```python
with Session(wallet, max_spend="0.05") as session:

    # Token price — $0.001
    r = session.call("token_price", {"symbol": "ETH"})
    print(f"ETH: ${r['result']['price_usd']:,.2f}  ({r['result']['change_24h_pct']:+.2f}% 24h)")

    # Open interest — $0.002
    r = session.call("open_interest", {"symbol": "ETH"})
    print(f"ETH OI: ${r['result']['total_oi_usd']/1e9:.2f}B  ({r['result']['oi_change_24h_pct']:+.2f}% 24h)")

    # Orderbook depth — $0.002
    r = session.call("orderbook_depth", {"symbol": "ETHUSDT"})
    slip = next(d['slippage_pct'] for d in r['result']['depth'] if d['notional_usd'] == 250_000)
    print(f"ETH $250k slippage: {slip:.3f}%")

    # Funding rates — $0.003
    r = session.call("funding_rates", {"asset": "ETH"})
    for ex in r['result'].get("rates", [])[:2]:
        print(f"  {ex['exchange']}: {ex['funding_rate_pct']:+.4f}%/8h")

    print(f"\nTotal spent: {session.spent()}")
    print(f"Remaining:   {session.remaining()}")
```

Each `session.call()` handles the full x402 flow internally:

1. Checks your remaining budget against the tool's price (pre-flight, no payment yet)
2. POSTs to the gateway, receives a `402` with `{payment_id, amount_usdc, pay_to}`
3. Sends USDC on Stellar (~3–5s) or Base (~2s) — your wallet picks the network
4. Retries the request with the payment proof header
5. Returns the data

---

## Available Tools

| Tool | Price | Parameters | Returns |
|------|-------|-----------|---------|
| `token_price` | $0.001 | `symbol` (BTC, ETH, SOL…) | price_usd, change_24h_pct, market_cap_usd |
| `gas_tracker` | $0.001 | — | slow/standard/fast gwei, base_fee_gwei |
| `fear_greed_index` | $0.001 | `limit` (days of history, default 1) | value 0–100, value_classification, history[] |
| `token_market_data` | $0.001 | `token_a`, `token_b` | volume_24h_usd, market_cap_usd, price_usd |
| `wallet_balance` | $0.002 | `address`, `chain` (ethereum/stellar) | token balances |
| `whale_activity` | $0.002 | `token`, `min_usd` (default 100k) | large_transfers[] with exchange direction, total_volume_usd |
| `defi_tvl` | $0.002 | `protocol` (optional, e.g. "uniswap") | tvl, change_1d, change_7d, chains[] |
| `token_security` | $0.002 | `contract_address`, `chain` (ethereum/bsc) | risk_level, is_honeypot, buy_tax, sell_tax |
| `open_interest` | $0.002 | `symbol` (BTC, ETH…) | total_oi_usd, oi_change_1h_pct, oi_change_24h_pct, long_short_ratio, exchanges[] |
| `orderbook_depth` | $0.002 | `symbol` (e.g. ETHUSDT), `exchange` (binance/bybit) | best_bid, best_ask, spread_pct, slippage at $10k/$50k/$250k |
| `funding_rates` | $0.003 | `asset` (optional, e.g. "BTC") | funding_rate_pct, annualized_rate_pct, sentiment per exchange |
| `crypto_news` | $0.003 | `currencies` (e.g. "ETH,BTC"), `filter` (hot/new/rising) | headlines[] with title, url, sentiment, score |
| `yield_scanner` | $0.004 | `token`, `chain` (optional), `min_tvl` (default $1M) | top 10 pools by APY with protocol, tvl_usd, risk_level |
| `dune_query` | $0.005 | `query_id`, `limit` (default 25), `fast_only` (bool) | rows[], columns[], row_count from Dune Analytics |

Discover all tools dynamically:

```python
import httpx
tools = httpx.get(f"{GATEWAY}/tools").json()["tools"]
for t in tools:
    print(f"{t['name']:<22} ${t['price_usdc']}  — {t['description']}")
```

---

## Pre-trade signal check

Pull the market context your agent needs right before a decision — funding regime, OI momentum, sentiment, and whale activity — in one call, $0.008.

```python
from agentpay import faucet_wallet, Session

wallet = faucet_wallet()
with Session(wallet, testnet=True) as session:

    rates  = session.call("funding_rates",   {"asset": "ETH"})
    oi     = session.call("open_interest",   {"symbol": "ETH"})
    fg     = session.call("fear_greed_index", {})
    whales = session.call("whale_activity",  {"token": "ETH", "min_usd": 500000})

    avg_rate  = sum(e["funding_rate_pct"] for e in rates["result"]["rates"]) / len(rates["result"]["rates"])
    oi_24h    = oi["result"]["oi_change_24h_pct"]
    sentiment = fg["result"]["value_classification"]
    whale_vol = whales["result"]["total_volume_usd"]

    print(f"Funding:   {avg_rate:+.4f}%/8h  (~{avg_rate * 3 * 365:.0f}% APY if carried)")
    print(f"OI 24h:    {oi_24h:+.2f}%")
    print(f"Sentiment: {sentiment}")
    print(f"Whale vol: ${whale_vol:,.0f}")
    print(f"Cost:      {session.spent()}")
```

---

## Payment Options

AgentPay accepts USDC payments on two networks:

- **Stellar** — ~$0.00001 per tx, 5-second settlement (recommended for agents using the Python SDK)
- **Base** — ~$0.0001 per tx, 2-second settlement (EIP-3009 `transferWithAuthorization` on Base mainnet)

The gateway's `402` response advertises both options simultaneously. Clients pick the network that suits them — no configuration required on the tool side.

### Stellar Payment Verification

Stellar payments are verified on-chain via [Stellar Horizon](https://horizon.stellar.org). The gateway checks that the transaction exists, is confirmed, sends USDC to the gateway address, and the amount matches the quoted price — all without requiring agents to trust a third party.

Base payments use Mode B direct on-chain settlement: the client calls `transferWithAuthorization` on the USDC contract and sends the `tx_hash` in the `PAYMENT-SIGNATURE` header. The gateway verifies the receipt via JSON-RPC.

---

## MCP Server

AgentPay ships a Model Context Protocol server that gives Claude Desktop (and any MCP client) direct access to all 14 tools. Payments happen automatically in the background using Stellar USDC.

See **[README_MCP.md](README_MCP.md)** for setup instructions.

**Quick config** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "agentpay": {
      "command": "python",
      "args": ["/path/to/agentpay/gateway/mcp_server.py"],
      "env": {
        "STELLAR_SECRET_KEY": "S...",
        "AGENTPAY_GATEWAY_URL": "https://gateway-production-2cc2.up.railway.app"
      }
    }
  }
}
```

Or via npm:

```bash
npx @romudille/agentpay-mcp
```

---

## Without the SDK — raw HTTP

The x402 flow works with any HTTP client in any language.

```bash
GATEWAY="https://gateway-production-2cc2.up.railway.app"
AGENT_ADDR="G..."   # your Stellar public key

# 1. Call the tool → receive 402 payment challenge
RESPONSE=$(curl -s -X POST "$GATEWAY/tools/token_price/call" \
  -H "Content-Type: application/json" \
  -d "{\"parameters\":{\"symbol\":\"ETH\"},\"agent_address\":\"$AGENT_ADDR\"}")

PAYMENT_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['payment_id'])")
AMOUNT=$(echo $RESPONSE    | python3 -c "import sys,json; print(json.load(sys.stdin)['amount_usdc'])")
PAY_TO=$(echo $RESPONSE    | python3 -c "import sys,json; print(json.load(sys.stdin)['pay_to'])")

# 2. Send USDC on Stellar (memo = payment_id)
TX_HASH=$(python3 -c "
from stellar_sdk import Keypair, Server, Network, Asset, TransactionBuilder
kp = Keypair.from_secret('S...')
server = Server('https://horizon.stellar.org')
acct = server.load_account(kp.public_key)
tx = (TransactionBuilder(acct, Network.PUBLIC_NETWORK_PASSPHRASE, base_fee=100)
      .add_text_memo('$PAYMENT_ID'[:28])
      .append_payment_op('$PAY_TO', Asset('USDC','GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN'), '$AMOUNT')
      .set_timeout(30).build())
tx.sign(kp)
print(server.submit_transaction(tx)['hash'])
")

# 3. Retry with payment proof → get data
curl -s -X POST "$GATEWAY/tools/token_price/call" \
  -H "Content-Type: application/json" \
  -H "X-Payment: tx_hash=$TX_HASH,from=$AGENT_ADDR,id=$PAYMENT_ID" \
  -d "{\"parameters\":{\"symbol\":\"ETH\"},\"agent_address\":\"$AGENT_ADDR\"}"
```

---

## Session API reference

```python
# Pre-check price without paying
price = session.estimate("dune_query")       # "$0.005"

# Check headroom before committing
if not session.would_exceed("0.005"):
    result = session.call("dune_query", {"query_id": 3810512, "limit": 10})

# Access spend state mid-session
session.spent()        # "$0.004"
session.remaining()    # "$0.046"

# Full breakdown after session closes
summary = session.summary()
# {
#   "calls": 3,
#   "spent_usdc": "0.007",
#   "spent_fmt": "$0.007",
#   "remaining_fmt": "$0.043",
#   "breakdown": [
#     {"tool": "funding_rates",   "amount_usdc": "0.003", "tx_hash": "abc123..."},
#     {"tool": "open_interest",   "amount_usdc": "0.002", "tx_hash": "def456..."},
#     {"tool": "orderbook_depth", "amount_usdc": "0.002", "tx_hash": "ghi789..."}
#   ]
# }
```

---

## Run a demo

```bash
pip install agentpay-x402
```

```python
from agentpay import faucet_wallet, Session

# Free testnet wallet — no setup
wallet = faucet_wallet()

with Session(wallet, testnet=True) as s:
    r = s.call("fear_greed_index", {})
    print(f"Fear & Greed: {r['result']['value']} ({r['result']['value_classification']})")

    r = s.call("token_price", {"symbol": "ETH"})
    print(f"ETH: ${r['result']['price_usd']:,.2f}")

    print(f"Total cost: {s.spent()}")
```

---

## Architecture

```
agent (Python SDK)
    │
    │  POST /tools/{name}/call
    │  ← 402 {payment_options: {stellar: {...}, base: {...}}}
    │  → Stellar USDC payment (~3–5s)  OR  Base transferWithAuthorization (~2s)
    │  → retry with X-Payment or PAYMENT-SIGNATURE header
    │  ← 200 {result: ...}
    ▼
gateway (FastAPI on Railway)
    │
    ├── registry/registry.py   — 14-tool catalog with prices & dev wallets
    ├── gateway/stellar.py     — Stellar payment verification via Horizon
    ├── gateway/base.py        — Base payment verification via JSON-RPC
    └── gateway/main.py        — real API dispatchers
            ├── CoinGecko         token_price, token_market_data
            ├── Etherscan V2      gas_tracker, whale_activity, wallet_balance
            ├── DeFiLlama         defi_tvl, yield_scanner
            ├── alternative.me    fear_greed_index
            ├── Reddit            crypto_news
            ├── Dune Analytics    dune_query
            ├── GoPlus            token_security
            └── Binance+Bybit+OKX funding_rates, open_interest, orderbook_depth
```

**Fee model**: Gateway charges 15% (`GATEWAY_FEE_PERCENT=0.15`), forwards the rest to each tool developer's Stellar wallet. All payments settle on-chain in ~3–5 seconds.

---

## Discovery

### Directories & Listings

| Directory | Status |
|-----------|--------|
| [PyPI](https://pypi.org/project/agentpay-x402/) | ✅ agentpay-x402 v0.1.0 |
| [x402scout](https://x402scout.com) | ✅ indexed, health-checked every 15min |
| [Glama MCP](https://glama.ai/mcp/servers/romudille-bit/agentpay) | ✅ listed |
| [awesome-x402](https://github.com/xpaysh/awesome-x402) | ✅ listed |
| [npm](https://www.npmjs.com/package/@romudille/agentpay-mcp) | ✅ @romudille/agentpay-mcp |
| [402index.io](https://402index.io) | ✅ domain verified, 14 tools synced |
| [xpay.tools](https://xpay.tools) | 🔜 submission in progress |

### Agent-Readable Endpoints

AgentPay is discoverable by autonomous agents at standard well-known paths:

| Endpoint | Purpose |
|----------|---------|
| `/.well-known/agentpay.json` | AgentPay manifest |
| `/.well-known/agent.json` | A2A agent card |
| `/.well-known/l402-services` | 402index.io discovery format |
| `/llms.txt` | LLM-readable service description (tools + integration guide) |
| `/sitemap.xml` | 17-URL sitemap |

Any x402-compatible agent can discover and use AgentPay tools without human setup.
