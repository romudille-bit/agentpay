# Arbitrum x402 Radar

Curated, usage-ranked discovery + 0%-fee on-chain settlement for x402 tools on the
**Arbitrum stack** (Arbitrum One, Sepolia, Robinhood Chain). Built on AgentPay's live
x402 gateway. See `SUBMISSION.md` for the buildathon write-up.

## Why

Arbitrum has no x402 directory of its own — discovery happens only through Coinbase's
Bazaar, which is generic, stub-polluted, and blind to Robinhood Chain. The Radar curates
and usage-ranks the Arbitrum-stack subset and routes real paying agent traffic to listed
projects at 0% gateway fee. `RadarSplit` is deployed live on Robinhood Chain (which Bazaar
can't index); the Robinhood discovery crawl is the next increment.

## Components

| File | Role |
|------|------|
| `gateway/radar.py` | Discovery core — pure `parse_resources` / `filter_chain` / `decide` / `rank`; chain identity (`CHAIN_NETWORKS`, `arbitrum-stack` group). |
| `gateway/routes/discovery.py` | `GET /discovery/arbitrum` (JSON API) + `GET /radar` (HTML leaderboard). Bounded cache, rate-limited, `RADAR_ENABLED` flag. |
| `gateway/radar_settle.py` | Verifies the on-chain `RadarSplit.Settled` event (contract, payer, developer, amount) via JSON-RPC. |
| `contracts/RadarSplit.sol` | Atomic, non-custodial split settlement; 0% default fee; payer-namespaced replay guard. |
| `tools/radar_demo.py` | End-to-end: need + budget → discover → recommend → settle plan. |
| `plugins/agentpay/bin/agentpay-route` | Standalone router with `--chain` (ships in the Claude Code plugin). |

## Run

```bash
# Discovery API + leaderboard (gateway must be running)
uvicorn gateway.main:app --port 8001
curl "http://localhost:8001/discovery/arbitrum?need=funding%20rates&chain=arbitrum-stack"
open  http://localhost:8001/radar

# End-to-end demo (offline against a fixture)
python3 tools/radar_demo.py "funding rates" --chain arbitrum-stack --fixture tests/fixtures/bazaar.json

# Standalone router
python3 plugins/agentpay/bin/agentpay-route "token security" --budget 0.004 --chain arbitrum
```

## Test

```bash
# Python (discovery + settlement verifier)
pytest tests/test_radar.py tests/test_radar_settle.py     # 22 tests

# Contract
cd contracts && forge test                                # 18 tests (incl. fuzz, reentrancy, griefing)
```

## API

`GET /discovery/arbitrum`
- `need` — query string (e.g. `funding rates`)
- `budget` — max USDC (default `0.01`)
- `chain` — `arbitrum-stack` | `arbitrum` | `arbitrum-sepolia` | `robinhood`
- Returns: `{need, chain, budget_usd, count, results[], recommendation}` — each result has
  `name, url, price_usd, network, pay_to, payers30d, calls30d, quality, flags`.

`POST /discovery/arbitrum/verify` — the third act: confirm a RadarSplit settlement.
- Body: `{tx_hash, payment_id, payer, developer, amount_usdc, chain}`
- Verifies the on-chain `Settled` event against the CANONICAL contract for the chain
  (all fields, not just paymentId) and **consumes the tx hash** — one settlement can't
  be presented twice.
- Config: `RADAR_CONTRACT_<CHAIN>` + `RADAR_RPC_<CHAIN>` env vars (503 until set).
- Returns `{success, reason, dev_amount, fee, tx_hash, chain, contract}`.

## Deploy

See `contracts/DEPLOY.md` (Arbitrum Sepolia, step by step). Settlement is non-custodial;
fee defaults to 0 bps (100% to the project), capped at 15%.
