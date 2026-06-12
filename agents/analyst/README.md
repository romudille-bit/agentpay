# Flagship analyst agent

An autonomous market analyst running on AgentPay's own rails **as a real
customer**: published SDK, production gateway, its own funded wallet, a hard
per-run budget cap. The "be your own first best customer" play — its daily
paid calls are the live proof (and the data behind `/ledger`).

## What a run does

1. **Plan** the day's work: free intel (fear/greed, funding, market snapshot)
   plus paid `pre_trade_check` verdicts on the majors.
2. **Estimate** the whole plan via `POST /v1/plan/estimate` before spending;
   trims paid steps if it doesn't fit the cap.
3. **Execute** — free tools first, then buys verdicts until the cap says stop.
4. **Publish** a market note + the spending receipt to stdout
   (`FLAGSHIP_NOTE {json}` line is machine-readable for the ledger).

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
