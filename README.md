# AgentPay — your agent is only as smart as its data.

AgentPay is an open x402 payment gateway that lets AI agents autonomously access real crypto data tools using USDC on Stellar.

No subscriptions. No API keys. No human in the loop.
Agents discover tools, pay per call ($0.001–$0.005), and get real data back — all within a hard budget cap.

→ **9 live tools**: token prices, whale activity, gas tracker, DeFi TVL, Fear & Greed, Dune queries and more
→ **Budget-aware Session**: agents estimate costs, track spend, never exceed budget
→ **x402 protocol**: works with any x402-compatible agent
→ **Stellar settlement**: 5-second finality, $0.00001 fees

**Try it in 60 seconds:**
```bash
curl https://gateway-production-2cc2.up.railway.app/faucet
```

Or open the browser faucet: `https://gateway-production-2cc2.up.railway.app/faucet/ui`

**Live gateway**: `https://gateway-production-2cc2.up.railway.app`

---

## Quickstart — 3 steps

### Step 1: Get a funded test wallet

One call gives you a ready-to-use Stellar testnet wallet with 5 USDC pre-loaded:

```bash
curl https://gateway-production-2cc2.up.railway.app/faucet
```

Or use the [browser faucet](https://gateway-production-2cc2.up.railway.app/faucet/ui) — click "Get Test Wallet", copy the snippet, run it.

---

### Step 2: Create a Session with a budget

```python
from agent.wallet import AgentWallet, Session, BudgetExceeded

wallet = AgentWallet(
    secret_key="S...",       # your Stellar secret key
    network="testnet",       # or "mainnet"
)

GATEWAY = "https://gateway-production-2cc2.up.railway.app"

with Session(wallet=wallet, gateway_url=GATEWAY, max_spend="0.05") as session:
    print(f"Balance:  {wallet.get_usdc_balance()} USDC")
    print(f"Budget:   {session.remaining()} remaining")
```

The `Session` enforces a hard USDC cap across all calls. It raises `BudgetExceeded` before any payment goes out if a tool would push you over, and automatically falls back to the next-cheapest tool in the same category when the preferred one is too expensive.

---

### Step 3: Call tools — payment is automatic

```python
with Session(wallet=wallet, gateway_url=GATEWAY, max_spend="0.05") as session:

    # Token price — $0.001
    r = session.call("token_price", {"symbol": "ETH"})
    print(f"ETH: ${r['price_usd']:,.2f}  ({r['change_24h_pct']:+.2f}% 24h)")

    # Fear & Greed Index — $0.001
    r = session.call("fear_greed_index", {"limit": 1})
    print(f"Sentiment: {r['value']}/100 — {r['value_classification']}")

    # DeFi TVL — $0.002
    r = session.call("defi_tvl", {"protocol": "aave"})
    print(f"Aave TVL: ${r['tvl'] / 1e9:.1f}B  ({r['change_1d']:+.1f}% 24h)")

    # Crypto news — $0.003
    r = session.call("crypto_news", {"currencies": "ETH", "filter": "hot"})
    for h in r["headlines"][:3]:
        print(f"  [{h['sentiment']:>7}] {h['title'][:55]}")

    print(f"\nTotal spent: {session.spent()}")
    print(f"Remaining:   {session.remaining()}")
```

Each `session.call()` handles the full x402 flow internally:

1. Checks your remaining budget against the tool's price (pre-flight, no payment yet)
2. POSTs to the gateway, receives a `402` with `{payment_id, amount_usdc, pay_to}`
3. Sends USDC on Stellar — ~2–3 seconds on testnet
4. Retries the request with `X-Payment: tx_hash=<hash>,from=<addr>,id=<payment_id>`
5. Returns the data

---

## Available Tools

| Tool | Price | Parameters | Returns |
|------|-------|-----------|---------|
| `token_price` | $0.001 | `symbol` (BTC, ETH, SOL…) | price_usd, change_24h_pct, market_cap_usd |
| `gas_tracker` | $0.001 | — | slow/standard/fast gwei, base_fee_gwei |
| `fear_greed_index` | $0.001 | `limit` (days of history, default 1) | value 0–100, value_classification, history[ ] |
| `wallet_balance` | $0.002 | `address`, `chain` (ethereum/stellar) | token balances |
| `whale_activity` | $0.002 | `token`, `min_usd` (default 100k) | large_transfers[ ], total_volume_usd |
| `defi_tvl` | $0.002 | `protocol` (optional, e.g. "uniswap") | tvl, change_1d, change_7d, chains[ ] |
| `dex_liquidity` | $0.003 | `token_a`, `token_b` | volume_24h_usd, market_cap_usd, ath_usd |
| `crypto_news` | $0.003 | `currencies` (e.g. "ETH,BTC"), `filter` (hot/new/rising) | headlines[ ] with title, url, sentiment, score |
| `dune_query` | $0.005 | `query_id`, `limit` (default 25) | rows[ ], columns[ ], row_count from Dune Analytics |

Discover all tools dynamically:

```python
import httpx
tools = httpx.get(f"{GATEWAY}/tools").json()["tools"]
for t in tools:
    print(f"{t['name']:<22} ${t['price_usdc']}  — {t['description']}")
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

# 2. Send USDC on Stellar (memo = first 28 chars of payment_id)
TX_HASH=$(python3 -c "
from stellar_sdk import Keypair, Server, Network, Asset, TransactionBuilder
kp = Keypair.from_secret('S...')
server = Server('https://horizon-testnet.stellar.org')
acct = server.load_account(kp.public_key)
tx = (TransactionBuilder(acct, Network.TESTNET_NETWORK_PASSPHRASE, base_fee=100)
      .add_text_memo('$PAYMENT_ID'[:28])
      .append_payment_op('$PAY_TO', Asset('USDC','GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5'), '$AMOUNT')
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
#   "spent_usdc": "0.006",
#   "spent_fmt": "$0.006",
#   "remaining_fmt": "$0.044",
#   "breakdown": [
#     {"tool": "token_price", "amount_usdc": "0.001", "tx_hash": "abc123..."},
#     {"tool": "gas_tracker",  "amount_usdc": "0.001", "tx_hash": "def456..."},
#     {"tool": "defi_tvl",     "amount_usdc": "0.002", "tx_hash": "ghi789..."}
#   ]
# }
```

---

## Run the demo

```bash
git clone <this-repo> && cd agentpay
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Copy env and add your Stellar secret key
cp .env.example .env

# Run the full 5-tool ETH analysis against the live gateway
AGENTPAY_GATEWAY_URL=https://gateway-production-2cc2.up.railway.app \
  python agent/budget_demo.py
```

Expected output: ETH price, gas, DEX liquidity, whale moves, and Dune onchain data — all paid for autonomously in ~$0.012 USDC.

---

## Architecture

```
agent (Python SDK)
    │
    │  POST /tools/{name}/call
    │  ← 402 {payment_id, amount_usdc, pay_to}
    │  → Stellar USDC payment (~3s)
    │  → retry with X-Payment header
    │  ← 200 {result: ...}
    ▼
gateway (FastAPI on Railway)
    │
    ├── registry/registry.py   — 9-tool catalog with prices & dev wallets
    ├── gateway/stellar.py     — payment verification via Stellar Horizon
    └── gateway/main.py        — real API dispatchers
            ├── CoinGecko      token_price, dex_liquidity
            ├── Etherscan V2   gas_tracker, whale_activity, wallet_balance
            ├── DeFiLlama      defi_tvl
            ├── alternative.me fear_greed_index
            ├── Reddit         crypto_news
            └── Dune Analytics dune_query
```

**Fee model**: Gateway charges 15% (`GATEWAY_FEE_PERCENT=0.15`), forwards the rest to each tool developer's Stellar wallet. All payments settle on-chain in ~5 seconds.

> **Note:** AgentPay currently uses the x402 pay-first pattern with classic Stellar PAYMENT ops. OZ Facilitator (verify-first, Soroban SAC) migration planned for v2.

---

## Discovery

All 9 AgentPay tools are indexed on [x402scout](https://x402scout.com) under `network: stellar-testnet`. Any agent that queries the x402 discovery catalog can find and call these tools without prior configuration.
