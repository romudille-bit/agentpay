# AgentPay — ![tests](https://github.com/romudille-bit/agentpay/actions/workflows/test.yml/badge.svg)

*If you are wondering how autonomous software entities discover, trust, pay, meter, and coordinate with each other safely —*

**AgentPay is the economic intelligence layer for MCP servers and AI agents.**

Agents spend money. Most don't know how much, or why, until the session ends and the bill arrives.

AgentPay gives agents economic intelligence — the ability to reason about cost while they work, not after.

It starts with a budget. Every session opens with a hard cap enforced at the payment layer — not in code a model can ignore, but at the point where money moves. The agent knows from the first call exactly what it has to spend.

Before calling a tool, it knows what that call costs. Mid-task, it can check what's left and route to a cheaper alternative if the math doesn't work. When the session ends, a receipt captures every call, every cost, every decision — not a debug log, but proof of economic accountability.

The developer sees all of it: spending patterns per agent, anomaly flags when something loops or spikes, policy controls that enforce exactly which tools an agent can use and how much it can spend on each.

The result is an agent that doesn't just have a budget. It knows how to use one.

**Start free:** 18 tools, no USDC needed, no wallet setup required.  
**Live gateway:** `https://agentpay.tools`

---

## Install

```bash
pip install agentpay-x402            # core (Stellar)
pip install "agentpay-x402[base]"    # + pay tools that settle on Base
```

---

## Quickstart — 3 lines, zero setup

17 free tools. No USDC, no wallet, no API keys, no human. `quickstart()` registers
an agent, mints a wallet, and returns a ready, budget-capped session.

```python
from agentpay import quickstart

s = quickstart()                                   # registers + mints a wallet
print(s.call("token_price", {"symbol": "ETH"})["result"]["price_usd"])
print(s.spending_summary())                        # receipt: every call, cost, tx
```

Set a hard budget, or bring your own funded wallet to pay for tools:

```python
s = quickstart(max_spend="0.50")                   # cap this run at $0.50
s = quickstart(secret_key="S...", base_key="0x...")  # your wallet (Stellar + Base)
```

Every call is session-tracked, and the cap is enforced **before** any payment is signed.

---

## 18 Tools (17 Free)

Every call is session-tracked — you get a receipt showing every tool called, every cost, and every timestamp.

| Tool | Parameters | Returns |
|------|-----------|---------|
| `url_reader` | `url` | Clean markdown content of any web page |
| `web_search` | `query` | Top 5 results with full content |
| `market_snapshot` | — | S&P 500, Treasury yield, BTC, ETH, gas in one call |
| `token_price` | `symbol` (BTC, ETH, SOL…) | price_usd, change_24h_pct, market_cap_usd |
| `gas_tracker` | — | slow/standard/fast gwei, base_fee_gwei |
| `fear_greed_index` | `limit` (days of history, default 1) | value 0–100, value_classification, history[] |
| `token_market_data` | `token_a`, `token_b` | volume_24h_usd, market_cap_usd, price_usd |
| `wallet_balance` | `address`, `chain` (ethereum/stellar) | token balances |
| `whale_activity` | `token`, `min_usd` (default 100k) | large_transfers[] with direction, total_volume_usd |
| `defi_tvl` | `protocol` (optional, e.g. "uniswap") | tvl, change_1d, change_7d, chains[] |
| `token_security` | `contract_address`, `chain` | risk_level, is_honeypot, buy_tax, sell_tax |
| `open_interest` | `symbol` (BTC, ETH…) | total_oi_usd, oi_change_1h/24h_pct, long_short_ratio |
| `orderbook_depth` | `symbol` (e.g. ETHUSDT) | best_bid/ask, spread_pct, slippage at $10k/$50k/$250k |
| `funding_rates` | `asset` (optional) | funding_rate_pct, annualized_rate_pct, sentiment per exchange |
| `crypto_news` | `currencies` (e.g. "ETH,BTC"), `filter` | headlines[] with title, url, sentiment, score |
| `yield_scanner` | `token`, `chain` (optional), `min_tvl` | top 10 pools by APY with protocol, tvl_usd, risk_level |
| `dune_query` | `query_id`, `limit`, `fast_only` | rows[], columns[], row_count from Dune Analytics |
| `session_create` | `agent_address`, `max_spend`, `label` | session_id, budget config, gateway_url, receipt — **$0.001** |

---

## Session Intelligence

This is the economic intelligence layer in practice. The Session gives your agent — and you — real visibility into what happened, what it cost, and why.

```python
from agentpay import quickstart, BudgetExceeded

# quickstart() registers + mints a wallet; the returned session is also a
# context manager, so you can `with` it for a printed receipt on exit.
# Budget caps are exact: max_spend=0.10 (float) == "0.10" (str).
with quickstart(max_spend=0.10) as session:

    # Reason about cost before committing (use the *_usd Decimals for comparisons)
    if session.would_exceed(session.tool_cost_usd("dune_query")):
        alt = session.suggest_cheaper("dune_query")   # {"name": ..., "price": ...}

    # Call a tool — budget enforced before any payment is signed
    r = session.call("token_price", {"symbol": "ETH"})
    r.data["price_usd"]    # inner tool output  (r["result"]["price_usd"] still works)
    r.cost                 # payment amount, e.g. "0"
    r.network              # settlement chain, e.g. "stellar-mainnet" / "base"

    session.remaining_usd()   # Decimal('0.10')

    # For an external x402 tool that offers several chains, pick one:
    # session.call("https://some-x402-tool/endpoint", {}, chain="base")

    # Full receipt — every call, cost, tx hash, and settlement chain
    print(session.spending_summary())
    # {
    #   "calls": 1, "spent": "$0", "remaining": "$0.1", "budget": "$0.1",
    #   "breakdown": [
    #     {"tool": "token_price", "cost": "Free", "tx_hash": "", "network": "stellar-mainnet"}
    #   ]
    # }
```

### Policy parameters

Control exactly what your agent is allowed to do:

```python
from agentpay import AgentWallet, Session

wallet = AgentWallet(secret_key="S...", network="mainnet")   # or quickstart()'s minted wallet
with Session(wallet,
             gateway_url="https://agentpay.tools",
             max_spend=0.10,
             allowed_tools=["token_price", "gas_tracker", "web_search"],
             max_per_tool={"dune_query": 0.02},
             rate_limit=10,                # max 10 calls/min
             prefer_chain="base") as session:   # Base is the default; pass "stellar" to override
    ...
```

`BudgetExceeded` fires before any payment goes out if a tool would push you over the cap, isn't on the allowlist, or exceeds its per-tool limit.

---

## Example: Market intelligence agent

Five free tools, one session, full receipt.

```python
from agentpay import quickstart

with quickstart() as session:

    snapshot = session.call("market_snapshot", {})
    rates    = session.call("funding_rates",    {"asset": "ETH"})
    oi       = session.call("open_interest",    {"symbol": "ETH"})
    fg       = session.call("fear_greed_index", {})
    whales   = session.call("whale_activity",   {"token": "ETH", "min_usd": 500_000})

    m = snapshot["result"]
    print(f"S&P:       {m['sp500_price']:,.0f}  ({m['sp500_change_pct']:+.2f}%)")
    print(f"ETH:       ${m['eth_price_usd']:,.0f}")
    print(f"Gas:       {m['gas_standard_gwei']} gwei")

    avg_rate = sum(e["funding_rate_pct"] for e in rates["result"]["rates"]) / len(rates["result"]["rates"])
    print(f"Funding:   {avg_rate:+.4f}%/8h")
    print(f"OI 24h:    {oi['result']['oi_change_24h_pct']:+.2f}%")
    print(f"Sentiment: {fg['result']['value_classification']}")
    print(f"Whale vol: ${whales['result']['total_volume_usd']:,.0f}")

    print(session.spending_summary())
```

---

## Use it in your agent

### Claude Code plugin (one command)

```
/plugin marketplace add romudille-bit/agentpay
/plugin install agentpay@agentpay
```

Installs the **`agentpay-route`** skill — your agent finds, judges, and pays for the best paid
x402 tool within a budget — plus the 17 free tools. No keys needed to route.

### MCP server (any runtime)

Self-contained — pure Node, no Python, no repo, no wallet:

```bash
npx -y @romudille/agentpay-mcp
```

```json
{
  "mcpServers": {
    "agentpay": {
      "command": "npx",
      "args": ["-y", "@romudille/agentpay-mcp"]
    }
  }
}
```

Exposes the 17 free tools **and** a `route` tool (buyer-side routing). Listed on
[Glama](https://glama.ai/mcp/servers/romudille-bit/agentpay).

### Buyer-side routing — find & pay for the best tool, within a budget

When an agent needs a paid tool, AgentPay discovers the options across the x402 marketplace,
drops the fake/empty stubs, ranks by **real usage** (not price), and recommends the cheapest one
that actually works — within a budget. The agent pays the provider **directly** (peer-to-peer,
no custody) and keeps a verifiable receipt.

```bash
agentpay-route "funding rates" --budget 0.01   # ranked candidates + a recommendation
```

---

## Paid tool: session_create

One tool costs money today: `session_create` ($0.001 USDC per session). It opens a budget-capped session with a hard `max_spend` limit — for autonomous agents that need spend enforcement across multiple calls. All 17 data tools remain free.

When metered inference ships, it works through the same Session interface — your agent checks cost, decides if it's worth it, and pays in USDC on Stellar or Base.

```python
# Future — inference as a Session tool
remaining = session.remaining()
infer_cost = session.tool_cost("inference")   # e.g. "$0.02"

if remaining >= infer_cost:
    result = session.call("inference", {"prompt": "...", "model": "claude-haiku"})
else:
    result = session.call("url_reader", {"url": summary_url})  # cheaper path
```

To fund a wallet for `session_create`: send USDC to a Stellar wallet (`S...` key, issuer `GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN`) or a Base wallet (`0x...`, contract `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`).

---

## Architecture

AgentPay is an x402 payment gateway and economic intelligence layer — agents call tools within a hard budget cap, pay USDC on-chain when tools cost money, and accumulate a full session receipt as they work. Free tools skip the payment step entirely; the session tracking and cost awareness are always on.

```
agent (Python SDK)
    │
    │  POST /tools/{name}/call
    │  ← 200 {result: ...}              ← free tools return directly
    │  ← 402 {payment_id, amount, ...}  ← paid tools (inference, coming soon)
    │  → USDC on Stellar (~3–5s) or Base (~2s)
    │  → retry with X-Payment header
    │  ← 200 {result: ...}
    ▼
gateway (FastAPI on Railway)
    │
    ├── registry/registry.py   — 18-tool catalog (17 free, session_create $0.001)
    ├── gateway/stellar.py     — Stellar payment verification via Horizon
    ├── gateway/base.py        — Base payment verification via JSON-RPC
    └── gateway/services/tools_runtime.py — real API dispatchers
            ├── Jina Reader       url_reader
            ├── Jina Search       web_search
            ├── Yahoo+CoinGecko   market_snapshot
            ├── CoinGecko         token_price, token_market_data
            ├── Etherscan V2      gas_tracker, whale_activity, wallet_balance
            ├── DeFiLlama         defi_tvl, yield_scanner
            ├── alternative.me    fear_greed_index
            ├── Reddit            crypto_news
            ├── Dune Analytics    dune_query
            ├── GoPlus            token_security
            └── Binance+Bybit+OKX funding_rates, open_interest, orderbook_depth
```

---

## Discovery

| Directory | Status |
|-----------|--------|
| [PyPI](https://pypi.org/project/agentpay-x402/) | ✅ agentpay-x402 |
| [x402scout](https://x402scout.com) | ✅ indexed, health-checked every 15min |
| [Glama MCP](https://glama.ai/mcp/servers/romudille-bit/agentpay) | ✅ listed |
| [awesome-x402](https://github.com/xpaysh/awesome-x402) | ✅ listed |
| [npm](https://www.npmjs.com/package/@romudille/agentpay-mcp) | ✅ @romudille/agentpay-mcp |
| [402index.io](https://402index.io) | ✅ domain verified, 17 tools synced |
| Coinbase Bazaar | ✅ indexed (`session_create`, Base) |
| Claude Code plugin | ✅ `/plugin marketplace add romudille-bit/agentpay` |
| [xpay.tools](https://xpay.tools) | 🔜 submission in progress |

**Agent-readable endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `/.well-known/agentpay.json` | AgentPay manifest |
| `/.well-known/agent.json` | A2A agent card |
| `/llms.txt` | LLM-readable service description |
| `/.well-known/l402-services` | 402index.io discovery format |
