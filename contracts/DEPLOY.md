# RadarSplit — deploy runbook (Arbitrum Sepolia)

Step-by-step to deploy `RadarSplit.sol` to **Arbitrum Sepolia** (the hard qualifier),
verify it, and produce one real `Settled` tx for the demo/video. Robinhood Chain is
the same flow with different env values (see the last section) — keep it timeboxed.

All commands are run by **you**, with **your** funded testnet key. This repo never
holds keys or broadcasts. Estimated time: ~15 min once faucets come through.

---

## 0. Prerequisites

```bash
# Foundry (forge + cast). If `forge --version` fails:
curl -L https://foundry.paradigm.xyz | bash && foundryup

cd ~/Projects/agentpay/contracts

# Fetch deps (not committed; lib/ is gitignored)
forge install foundry-rs/forge-std
forge install OpenZeppelin/openzeppelin-contracts@v5.1.0

forge build        # should compile clean
forge test         # 18 tests should pass
```

---

## 1. Create two keystore accounts (encrypted; never paste raw keys on the CLI)

You need two roles:
- **deployer** — deploys the contract and becomes owner (the gateway wallet role).
- **agent** — the paying agent in the settle test.

For a hackathon testnet, the same key can play both; create at least `deployer`.

```bash
# Option A — import an existing testnet private key (you'll be prompted for it + a password)
cast wallet import deployer --interactive
cast wallet import agent --interactive

# Option B — generate fresh throwaway testnet keys
cast wallet new            # prints an address + private key; import it as above
```

Get each address for funding:

```bash
cast wallet address --account deployer
cast wallet address --account agent
```

> Never reuse a mainnet key. These are throwaway testnet accounts.

---

## 2. Fund the accounts on Arbitrum Sepolia

You need **Arbitrum Sepolia ETH** (gas) for `deployer`, and **testnet USDC** for `agent`
(the settle test). Faucets (pick any that works — they rate-limit):

- ETH (gas): https://www.alchemy.com/faucets/arbitrum-sepolia · https://faucets.chain.link/arbitrum-sepolia · https://faucet.quicknode.com/arbitrum/sepolia
- USDC: https://faucet.circle.com (select **Arbitrum Sepolia**, ~20 USDC / 2h)

Confirm funds arrived:

```bash
RPC=https://sepolia-rollup.arbitrum.io/rpc
USDC=0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d

# gas balance (wei) for deployer
cast balance --rpc-url $RPC "$(cast wallet address --account deployer)"

# USDC balance (6dp) for agent — should be > 0 after the Circle faucet
cast call $USDC "balanceOf(address)(uint256)" \
  "$(cast wallet address --account agent)" --rpc-url $RPC
```

---

## 3. Deploy RadarSplit

```bash
cd ~/Projects/agentpay/contracts

# Owner + fee recipient = your gateway wallet. 0% promo fee for the Arbitrum stack.
GW=$(cast wallet address --account deployer)        # or your real gateway wallet
export RADAR_USDC=0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d
export RADAR_OWNER=$GW
export RADAR_FEE_RECIPIENT=$GW
export RADAR_FEE_BPS=0

forge script script/Deploy.s.sol \
  --rpc-url https://sepolia-rollup.arbitrum.io/rpc \
  --account deployer \
  --broadcast
```

The console prints `RadarSplit deployed at: 0x...`. Copy that address — call it
`$RADAR`. (It's also saved under `broadcast/Deploy.s.sol/421614/run-latest.json`.)

```bash
export RADAR=0x...   # the deployed address
```

### (Optional) verify the source on Arbiscan

```bash
# needs a free Arbiscan API key: https://arbiscan.io/myapikey
forge verify-contract "$RADAR" src/RadarSplit.sol:RadarSplit \
  --chain arbitrum-sepolia \
  --constructor-args "$(cast abi-encode 'c(address,address,address,uint16)' \
      $RADAR_USDC $RADAR_OWNER $RADAR_FEE_RECIPIENT 0)" \
  --etherscan-api-key "$ARBISCAN_API_KEY"
```

---

## 4. Confirm the deploy is healthy (catches the PUSH0/ArbOS class of failure)

A contract that deploys but reverts on first call is the failure mode we pinned
`evm_version = paris` to avoid. These read-only calls prove it's live and correct:

```bash
RPC=https://sepolia-rollup.arbitrum.io/rpc
cast call $RADAR "usdc()(address)"      --rpc-url $RPC   # → RADAR_USDC
cast call $RADAR "owner()(address)"     --rpc-url $RPC   # → your gateway wallet
cast call $RADAR "feeBps()(uint16)"     --rpc-url $RPC   # → 0
cast call $RADAR "MAX_FEE_BPS()(uint16)" --rpc-url $RPC  # → 1500
```

If those return the expected values, the qualifier is cleared. ✅

---

## 5. Produce one real `Settled` tx (the demo/video money-shot)

Get the exact commands from the demo (it fills in a recommended project + amount):

```bash
cd ~/Projects/agentpay
RADAR_CONTRACT=$RADAR python3 tools/radar_demo.py "funding rates" \
  --chain arbitrum-sepolia --fixture tests/fixtures/bazaar.json
```

Then run the two `cast send` commands it prints (approve, then settle) with
`--account agent`. Minimal manual version:

```bash
RPC=https://sepolia-rollup.arbitrum.io/rpc
USDC=0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d
DEV=0x...            # a Radar-listed project's pay_to (from the demo output)
AMT=1000             # 0.001 USDC, atomic (6dp)
PID=0x$(openssl rand -hex 32)   # unpredictable payment id

# approve once
cast send $USDC "approve(address,uint256)" $RADAR $AMT --rpc-url $RPC --account agent

# settle → emits Settled(paymentId, payer, developer, devAmount, fee, feeRecipient)
cast send $RADAR "settle(bytes32,address,uint256)" $PID $DEV $AMT --rpc-url $RPC --account agent
```

Capture the settle tx hash from the output and open it:
`https://sepolia.arbiscan.io/tx/<txhash>` — that's the explorer proof for the video.

### (Optional) confirm with the gateway verifier

```bash
cd ~/Projects/agentpay && python3 - <<'PY'
import asyncio
from gateway.radar_settle import verify_radar_settlement
out = asyncio.run(verify_radar_settlement(
    tx_hash="0x<settle_tx>",
    contract="0x<RADAR>",
    payment_id="0x<PID>",
    payer="0x<agent_address>",
    developer="0x<DEV>",
    required_amount_atomic=1000,
    rpc_url="https://sepolia-rollup.arbitrum.io/rpc",
))
print(out)   # {'success': True, 'reason': 'ok', ...}
PY
```

---

## 6. Record the results

Drop these into the submission notes / README:
- `RadarSplit @ <address>` on Arbitrum Sepolia (+ Arbiscan link)
- the `Settled` tx hash (+ Arbiscan link)
- owner + feeBps confirmation from step 4

---

## Robinhood Chain testnet — bonus (timeboxed, droppable)

Reserved-prize upside on the sponsor's own chain. **Hard rule: do not let this eat the
day.** Arbitrum Sepolia already cleared the qualifier; if the faucet/RPC fights you, stop
and ship Sepolia-only. Reuses the **same `deployer` keystore** — no new account.

Chain facts: chain id **46630**, RPC `https://rpc.testnet.chain.robinhood.com/rpc`,
explorer (Blockscout) `https://explorer.testnet.chain.robinhood.com`, USDC
`0x5B6C7cAF7F99f99154fD8375ec935Fcf03F326f5`.

### R1 — Fund the deployer on Robinhood Chain

```bash
export PATH="$HOME/.foundry/bin:$PATH"
RRPC=https://rpc.testnet.chain.robinhood.com/rpc
GW=0x3312c6BE066AaEa646813365328E1893a6a2c156

# Faucet (gas token + test tokens): https://faucet.testnet.chain.robinhood.com/
# Paste $GW as the recipient. Then confirm gas arrived:
cast balance --rpc-url $RRPC -e $GW
# And USDC (for the settle step):
cast call 0x5B6C7cAF7F99f99154fD8375ec935Fcf03F326f5 \
  "balanceOf(address)(uint256)" $GW --rpc-url $RRPC
```

If the faucet only drips gas (not USDC), you can still deploy + prove the contract is
live with the read-only health checks in R3 — the settle in R4 just needs USDC too.

### R2 — Deploy

```bash
cd ~/Projects/agentpay/contracts
export RADAR_USDC=0x5B6C7cAF7F99f99154fD8375ec935Fcf03F326f5
export RADAR_OWNER=$GW
export RADAR_FEE_RECIPIENT=$GW
export RADAR_FEE_BPS=0

forge script script/Deploy.s.sol \
  --rpc-url https://rpc.testnet.chain.robinhood.com/rpc \
  --account deployer --broadcast

# copy the printed address:
export RHRADAR=0x...
```

### R3 — Health-check (proves it's live; catches any ArbOS/PUSH0 issue)

```bash
RRPC=https://rpc.testnet.chain.robinhood.com/rpc
cast call $RHRADAR "usdc()(address)"       --rpc-url $RRPC   # → USDC
cast call $RHRADAR "owner()(address)"      --rpc-url $RRPC   # → your wallet
cast call $RHRADAR "feeBps()(uint16)"      --rpc-url $RRPC   # → 0
cast call $RHRADAR "MAX_FEE_BPS()(uint16)" --rpc-url $RRPC   # → 1500
```

### R4 — One real `Settled` tx (skip if no USDC from the faucet)

```bash
RRPC=https://rpc.testnet.chain.robinhood.com/rpc
RUSDC=0x5B6C7cAF7F99f99154fD8375ec935Fcf03F326f5
DEV=0xF6b26695655Da539B87eB871b337B8713cAca00a     # reuse the same project recipient
AMT=1000
PID=0x$(openssl rand -hex 32)

cast send $RUSDC "approve(address,uint256)" $RHRADAR $AMT --rpc-url $RRPC --account deployer
cast send $RHRADAR "settle(bytes32,address,uint256)" $PID $DEV $AMT --rpc-url $RRPC --account deployer

# proof: 100% landed at the project
cast call $RUSDC "balanceOf(address)(uint256)" $DEV --rpc-url $RRPC
# explorer: https://explorer.testnet.chain.robinhood.com/tx/<settle_txhash>
```

### R5 — Record

Send the AgentPay session the **Robinhood contract address** + **settle tx hash**; they go
into `SUBMISSION.md` (the "Robinhood Chain deploy" line) as the reserved-prize evidence.

Optional source verification (Blockscout):
`forge verify-contract $RHRADAR src/RadarSplit.sol:RadarSplit --verifier blockscout
--verifier-url https://explorer.testnet.chain.robinhood.com/api`
