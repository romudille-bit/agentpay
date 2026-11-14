// What it actually took to fund a Base payment from NEAR, read back from 1Click and
// the chains. 1Click names the deposit and the refund or delivery transaction; the
// wait is the time between those two, so anyone can check a row.

import { fileURLToPath } from 'node:url';
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';

const here = fileURLToPath(new URL('..', import.meta.url));
export const ONECLICK = 'https://1click.chaindefuser.com/v0';
const NEARBLOCKS = 'https://api.nearblocks.io/v1';
const BASE_RPC = 'https://mainnet.base.org';
const UA = { 'user-agent': 'agentpay-near-check/0.1' };
const UNIT = { 'nep141:wrap.near': 'NEAR', 'nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1': 'USDC' };

/** Deposit addresses we sent to: the two that ran before receipts kept the address, plus any in receipts. */
export function attempts(root = here) {
  const seen = new Set([
    '8a48c15e5ce84d6b60a16e5dd5f966fbdb970e3bd1ec5adc42497fe76f2893de',
    '40085f1c4aea515350b5e58fdec68badd751690441cd0884e806218b7293ad42',
  ]);
  const dir = `${root}receipts`;
  if (existsSync(dir)) {
    for (const f of readdirSync(dir).filter((n) => n.endsWith('.json'))) {
      const a = JSON.parse(readFileSync(`${dir}/${f}`, 'utf8'))?.funding?.deposit_address;
      if (a) seen.add(a);
    }
  }
  return [...seen];
}

/** Block time of a NEAR or Base transaction, as an ISO string, or null. */
export async function txTime(hash, { fetchImpl = globalThis.fetch } = {}) {
  if (!hash) return null;
  try {
    if (hash.startsWith('0x')) {
      const rpc = (method, params) => fetchImpl(BASE_RPC, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) }).then((r) => r.json());
      const block = (await rpc('eth_getTransactionReceipt', [hash])).result?.blockNumber;
      const ts = block && (await rpc('eth_getBlockByNumber', [block, false])).result?.timestamp;
      return ts ? new Date(Number(ts) * 1000).toISOString() : null;
    }
    const tx = (await fetchImpl(`${NEARBLOCKS}/txns/${hash}`, { headers: UA }).then((r) => r.json()))?.txns?.[0];
    return tx?.block_timestamp ? new Date(Number(BigInt(tx.block_timestamp) / 1000000n)).toISOString() : null;
  } catch { return null; }
}

/** One table row. `sentAt` and `endedAt` are block times; without them the wait is unknown. */
export function row(address, status, { sentAt = null, endedAt = null } = {}) {
  const d = status?.swapDetails || {};
  return {
    address,
    sent_at: sentAt,
    ended_at: endedAt,
    seconds: endedAt && sentAt ? Math.max(0, Math.round((Date.parse(endedAt) - Date.parse(sentAt)) / 1000)) : null,
    status: status?.status ?? 'UNKNOWN',
    sent: d.amountInFormatted ?? d.depositedAmountFormatted ?? null,
    sent_unit: UNIT[status?.quoteResponse?.quoteRequest?.originAsset] ?? null,
    received: d.amountOutFormatted ?? null,
    reason: d.refundReason ?? null,
    quoted_eta_s: status?.quoteResponse?.quote?.timeEstimate ?? null,
    deposit_tx: d.originChainTxHashes?.[0]?.hash ?? null,
    end_tx: d.destinationChainTxHashes?.[0]?.hash ?? null,
  };
}

export async function funding(root = here, { fetchImpl = globalThis.fetch } = {}) {
  const out = [];
  for (const address of attempts(root)) {
    const status = await fetchImpl(`${ONECLICK}/status?depositAddress=${address}`).then((r) => r.json()).catch(() => null);
    const d = status?.swapDetails || {};
    const sentAt = await txTime(d.originChainTxHashes?.[0]?.hash, { fetchImpl });
    const endedAt = await txTime(d.destinationChainTxHashes?.[0]?.hash, { fetchImpl });
    out.push(row(address, status, { sentAt, endedAt }));
  }
  return out.sort((a, b) => String(a.sent_at).localeCompare(String(b.sent_at)));
}

if (fileURLToPath(import.meta.url) === process.argv[1]) {
  const rows = await funding();
  mkdirSync(`${here}docs`, { recursive: true });
  writeFileSync(`${here}docs/funding.json`, JSON.stringify(rows, null, 1));
  console.log(JSON.stringify({ attempts: rows.length, succeeded: rows.filter((r) => r.status === 'SUCCESS').length, waits_min: rows.map((r) => r.seconds == null ? null : Math.round(r.seconds / 60)) }));
}
