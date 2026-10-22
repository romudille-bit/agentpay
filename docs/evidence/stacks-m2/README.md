# Stacks Endowment grant — Milestone 2 evidence

Public copies of the evidence files cited in the Milestone 2 submission (29 Sep 2026, approved 22 Oct 2026). The submission referred to them by their working paths under `notes/`, which is not published; this folder is the public location.

| Cited in the submission | Public file | What it is |
|---|---|---|
| `notes/M2_MAINNET_RECEIPTS_2026-09-08.md` | [M2_MAINNET_RECEIPTS_2026-09-08.md](M2_MAINNET_RECEIPTS_2026-09-08.md) | The first three sBTC payments settled on Stacks mainnet from a capped `agentpay` Session, each followed by the over-cap refusal, with the Hiro verification per receipt |
| `notes/stacks_standard_clients_mainnet_2026-09-24.json` | [stacks_standard_clients_mainnet_2026-09-24.json](stacks_standard_clients_mainnet_2026-09-24.json) | sBTC payments to the gateway from two standard Stacks x402 clients (x402-stacks 2.0.3, @aibtc/mcp-server 1.71.0), with funding and sweep transactions |
| `notes/stacks_standard_clients_mainnet_stx_2026-09-26.json` | [stacks_standard_clients_mainnet_stx_2026-09-26.json](stacks_standard_clients_mainnet_stx_2026-09-26.json) | The same two clients paying in native STX |
| `notes/session_cap_proof_2026-09-25.json` | [session_cap_proof_2026-09-25.json](session_cap_proof_2026-09-25.json) | Server-side session cap on mainnet: session opened, two calls paid, third refused with the payer nonce unchanged and no mempool transaction |

Every transaction id resolves on `https://explorer.hiro.so/txid/0x<txid>?chain=mainnet`.

## Commit references

Two commits cited in the submission were rewritten when the branch was re-signed before pushing, so the cited hashes do not exist on `origin`. The content is identical; the public equivalents are:

| Cited | On `main` | Change |
|---|---|---|
| `1ac7ec7` | `e7b3cd3` | demo: one-line summary per settled call, full dicts behind `--verbose` |
| `3140033` | `7934e2f` | gateway: accept native STX from standard Stacks clients |

Walkthrough and configuration: [`docs/stacks-walkthrough.md`](../../stacks-walkthrough.md), [`docs/stacks-mainnet.md`](../../stacks-mainnet.md).
