# AgentPay — Cowork Session Starter

Read this file at the start of any Cowork or Claude chat session to get full context.
For Claude Code sessions, CLAUDE.md is auto-read instead.

---

## What AgentPay Is

x402 payment gateway — AI agents pay USDC on Stellar or Base to call real crypto data tools.
No API keys. No subscriptions. Pay per call. Budget-capped.

**Live since**: March 31, 2026 (Stellar mainnet). First tx: `29f59465cfed5620`
**Founder**: Valeria (velvetvau@gmail.com)
**Hackathon deadline**: April 13, 2026 — Stellar Hacks on DoraHacks

---

## Gateways

| Service | URL | Network |
|---------|-----|---------|
| Production | https://gateway-production-2cc2.up.railway.app | Stellar mainnet + Base mainnet |
| Testnet | https://gateway-testnet-production.up.railway.app | Stellar testnet (faucet enabled) |

---

## 12 Live Tools

| Tool | Price | API |
|------|-------|-----|
| token_price | $0.001 | CoinGecko |
| gas_tracker | $0.001 | Etherscan V2 |
| fear_greed_index | $0.001 | alternative.me |
| wallet_balance | $0.002 | Stellar Horizon / Etherscan V2 |
| whale_activity | $0.002 | Etherscan V2 |
| defi_tvl | $0.002 | DeFiLlama |
| token_security | $0.002 | GoPlus |
| dex_liquidity | $0.003 | CoinGecko |
| crypto_news | $0.003 | Reddit |
| funding_rates | $0.003 | Binance + Bybit + OKX |
| yield_scanner | $0.004 | DeFiLlama |
| dune_query | $0.005 | Dune Analytics |

---

## Key Wallets

| Role | Network | Address |
|------|---------|---------|
| Gateway | Stellar mainnet | GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2 |
| Gateway | Base mainnet | 0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7 |

USDC on Stellar mainnet: `GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN`
USDC on Base: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`

---

## Project Folder Structure

```
agentpay/                          ← mount THIS folder in Cowork
├── CONTEXT.md                     ← you are here (Cowork starter)
├── CLAUDE.md                      ← Claude Code quick reference (auto-read)
├── README.md                      ← public GitHub README
├── README_MCP.md                  ← MCP setup guide
│
├── docs/
│   ├── specs/
│   │   ├── AgentPay_Technical_Spec_v4.docx   ← latest technical spec
│   │   ├── AgentPay_Protocol_Spec_v2.docx    ← x402 protocol spec
│   │   └── AgentPay_TradingAgent_ValueProp.pdf
│
├── .claude/
│   ├── skills/
│   │   ├── agentpay-context/SKILL.md   ← full dev context for Claude Code
│   │   ├── cmo-agent/SKILL.md          ← CMO agent (marketing, content, growth)
│   │   ├── code-reviewer/SKILL.md      ← code review agent (security, bugs)
│   │   └── session-tips/SKILL.md       ← token/memory optimization guide
│   └── content/
│       └── twitter/                    ← drafted tweets (YYYY-MM-DD.md)
│
├── gateway/                       ← FastAPI gateway (Railway)
├── registry/                      ← 12-tool registry
├── agent/                         ← Python SDK + budget demo
└── npm/                           ← MCP npm wrapper
```

---

## Skills — When to Use Which

| Task | Skill to load |
|------|--------------|
| Any coding / debugging | `.claude/skills/agentpay-context/SKILL.md` |
| Tweets, Discord posts, growth | `.claude/skills/cmo-agent/SKILL.md` |
| Code review, security audit | `.claude/skills/code-reviewer/SKILL.md` |
| Context running low, new session | `.claude/skills/session-tips/SKILL.md` |

---

## Current Priorities (April 2026)

1. **Hackathon** — deadline April 13. DoraHacks submission covers 7 use cases.
2. **xpay.tools** — submit listing (awesome-x402 ✅ listed today)
3. **Twitter** — daily drafts auto-generated at 8am, review and post
4. **Discord** — Stellar + Anthropic servers, weekly drafts generated Mondays at 9am
5. **README** — always keep in sync; run `git push origin main` after local edits

---

## Discovery Status

| Directory | Status |
|-----------|--------|
| x402scout | ✅ indexed |
| Glama MCP | ✅ listed |
| 402index.io | ✅ 12 tools |
| awesome-x402 | ✅ listed |
| npm | ✅ v1.0.3 |
| xpay.tools | 🔜 in progress |

---

## Automated Tasks (running in background)

| Task | Schedule | What it does |
|------|----------|-------------|
| `agentpay-sync-docs` | Daily 9am | Syncs CLAUDE.md + SKILL.md with source code |
| `agentpay-daily-tweet` | Daily 8am | Drafts today's tweet → `.claude/content/twitter/` |
| `agentpay-weekly-discord` | Mondays 9am | Drafts Discord posts for Stellar + Anthropic servers |

---

## How to Start a Cowork Session

1. Mount `~/Downloads/agentpay` as your working folder
2. Tell Claude: "Read CONTEXT.md and let's work on AgentPay"
3. For marketing tasks, also say: "Load the CMO skill from .claude/skills/cmo-agent/SKILL.md"
4. For code tasks, also say: "Load the context skill from .claude/skills/agentpay-context/SKILL.md"

---

## Key Commands

```bash
# Run gateway locally
cd ~/Downloads/agentpay && source venv/bin/activate
uvicorn gateway.main:app --port 8001 --reload

# Deploy to Railway
railway up --service gateway

# Push local changes to GitHub
git push origin main

# Testnet faucet
curl https://gateway-testnet-production.up.railway.app/faucet
```
