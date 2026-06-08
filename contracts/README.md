# RadarSplit — AgentPay Arbitrum x402 Radar settlement

Atomic, non-custodial x402 settlement for Radar-listed projects on the Arbitrum
stack (Arbitrum One/Sepolia, Robinhood Chain). The payer's USDC is split in one
transaction between the developer and an optional gateway fee. The fee defaults
to **0 bps** for the Arbitrum/Robinhood ecosystem promo (100% to the project) and
is capped at 1500 bps (15%). Each `paymentId` settles at most once (replay guard).

Non-custodial by design: funds move payer → developer (+ payer → feeRecipient)
via `transferFrom`; the contract never holds a balance. This keeps AgentPay a
relay, not a custodian — and is the first on-chain piece of the parked
session-enforcement product (Model B).

## Setup

Dependencies are not committed; fetch them with Foundry:

```bash
cd contracts
forge install foundry-rs/forge-std
forge install OpenZeppelin/openzeppelin-contracts@v5.1.0
```

Remappings (`foundry.toml`): `@openzeppelin/=lib/openzeppelin-contracts/`.

## Build & test

```bash
forge build
forge test -vv        # 13 tests incl. a 256-run fuzz on the split math
```

## Deploy (run yourself — broadcasts a real tx, needs a funded key)

```bash
# Arbitrum Sepolia (chain 421614)
export RADAR_USDC=0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d
export RADAR_OWNER=0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7         # gateway wallet (owns setFeeConfig)
export RADAR_FEE_RECIPIENT=0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7   # gateway Base wallet
export RADAR_FEE_BPS=0
forge script script/Deploy.s.sol \
  --rpc-url https://sepolia-rollup.arbitrum.io/rpc \
  --account deployer --broadcast --verify

# Robinhood Chain testnet (chain 46630)
export RADAR_USDC=0x5B6C7cAF7F99f99154fD8375ec935Fcf03F326f5
forge script script/Deploy.s.sol \
  --rpc-url https://rpc.testnet.chain.robinhood.com/rpc \
  --account deployer --broadcast
```

## Interface

- `settle(bytes32 paymentId, address developer, uint256 amount)` — the paying agent
  (having approved USDC to this contract) splits `amount` to `developer` + fee;
  emits `Settled(paymentId, payer, developer, devAmount, fee, feeRecipient)`.
- `setFeeConfig(uint16 feeBps, address feeRecipient)` — owner only, capped at 15%.
- `isSettled(bytes32 paymentId, address payer) → bool` — replay guard is namespaced
  by payer, so the gateway adapter must verify all `Settled` fields (contract address,
  payer, developer, amount), not just paymentId.

The gateway verifies the `Settled` event over JSON-RPC (reusing the `gateway/base.py`
log-parsing pattern) to confirm payment before returning tool data.
