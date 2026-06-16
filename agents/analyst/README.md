# Flagship analyst agent

An autonomous market analyst running on AgentPay's own rails **as a real
customer**: published SDK, production gateway, its own funded wallet, a hard
per-run budget cap. The "be your own first best customer" play — its daily
paid calls are the live proof (and the data behind `/ledger`).

## What a run does

Each day the agent rotates through a **5-goal cycle** (`day.toordinal() % 5`):
`pretrade_majors` → `regime_brief` → `verified_route` → `pretrade_alts` →
`crowding_watch`. Two paid days (`pretrade_majors`, `verified_route`) interleaved
with free regime/crowding reads. `strategy_spec` (the BNB-hackathon backtest goal)
is **force-only** (`FLAGSHIP_GOAL=strategy_spec`) — never auto-rotates, never
auto-spends.

1. **Plan** the day's work and **estimate** the whole plan via
   `POST /v1/plan/estimate` before spending; trims paid steps if it doesn't fit the cap.
2. **Execute** — free intel first (fear/greed, funding, market snapshot), then the
   day's paid leg until the cap says stop:
   - `pretrade_*` days buy `pre_trade_check` verdicts ($0.01 each).
   - the **`verified_route`** day (`run_vetting`) buys ONE `verified_route` ($0.01):
     vet the marketplace → return the real, used provider. The autonomous, on-chain
     proof that the agent pays for trust before it spends.
   - `strategy_spec` (`run_strategy`, force-only) chains verified_route + paid CMC
     DEX data → a regime-gated mean-reversion spec **backtested over 180d with a
     buy-and-hold benchmark** (`backtest.py`).
3. **Publish** a market note + the spending receipt to stdout (`FLAGSHIP_NOTE` /
   `FLAGSHIP_VETTING` / `FLAGSHIP_STRATEGY` JSON lines) **and** `POST /v1/flagship/run`
   so `/ledger` can render the decision loop.

## Ledger reasoning — required config

The public `/ledger` shows on-chain receipts from `payment_logs` automatically, but
the **decision cards** (goal, regime, verdicts, the verified_route VETTED card) only
render when the run's reasoning is persisted. That needs:
- the **`flagship_runs` table** in Supabase (`db/migrations/flagship_runs.sql`,
  applied via the Supabase SQL Editor), and
- **`FLAGSHIP_INGEST_SECRET`** set to the *same value* on BOTH the gateway and this
  service. Unset on the gateway → `POST /v1/flagship/run` 404s → no reasoning stored
  (`runs_with_reasoning: 0`). Verify: that endpoint returns 401 (not 404), and after a
  run `/ledger.json` shows `runs_with_reasoning > 0`.

## Deploy (separate Railway service)

1. **Mint the identity once** (locally; keys go straight to Railway env vars):

   ```bash
   python3 -c "from stellar_sdk import Keypair; kp = Keypair.random(); print('FLAGSHIP_STELLAR_SECRET=' + kp.secret)"
   python3 -c "from eth_account import Account; a = Account.create(); print('FLAGSHIP_BASE_KEY=0x' + a.key.hex()); print('fund this address:', a.address)"
   ```

2. **Fund** the printed `0x` address with USDC on Base (suggest $5 ≈ 500 verdicts).

3. **Railway**: new service in this repo —
   - Root directory: `agents/analyst`
   - Start command: `python run.py`
   - Cron schedule: `0 13 * * *` (daily 13:00 UTC; pick your hour)
   - Variables: `FLAGSHIP_STELLAR_SECRET`, `FLAGSHIP_BASE_KEY`,
     optional `FLAGSHIP_MAX_SPEND` (default `0.25`),
     `FLAGSHIP_SYMBOLS` (default `BTC,ETH`)

4. **Dry-run locally** before scheduling:

   ```bash
   cd agents/analyst && pip install -r requirements.txt
   FLAGSHIP_STELLAR_SECRET=S... FLAGSHIP_BASE_KEY=0x... python run.py
   ```

## Instrumentation

The agent's wallet address appears on every `payment_logs` row it creates —
record it (and only it) as the flagship wallet so dashboards and the public
`/ledger` can split flagship traffic from organic. Exit code 1 = a run that
bought nothing; Railway cron surfaces it.
