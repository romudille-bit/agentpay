// Attach the on-chain settlement to every delivered hire. The marketplace settles a
// delivered hire as one USDC transfer from the payer to its treasury, so each hire is
// matched to the first unclaimed transfer of the same amount after it was signed.

import { fileURLToPath } from 'node:url';
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { NEAR_USDC } from './challenge.mjs';

const here = fileURLToPath(new URL('..', import.meta.url));
const NEARBLOCKS = 'https://api.nearblocks.io/v1';
const UA = { 'user-agent': 'agentpay-near-check/0.1' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Every successful USDC transfer the account made, oldest first. */
export async function transfers(accountId, { fetchImpl = globalThis.fetch, pages = 20 } = {}) {
  const out = [];
  let cursor = null;
  for (let i = 0; i < pages; i += 1) {
    const d = await fetchImpl(`${NEARBLOCKS}/account/${accountId}/receipts?per_page=25${cursor ? `&cursor=${cursor}` : ''}`, { headers: UA }).then((r) => r.json());
    for (const r of d?.txns || []) {
      if (r.receiver_account_id !== NEAR_USDC || !r.receipt_outcome?.status) continue;
      for (const a of r.actions || []) {
        if (a.method !== 'ft_transfer') continue;
        const args = JSON.parse(a.args);
        out.push({ at: Number(BigInt(r.block_timestamp) / 1000000n), amount: args.amount, to: args.receiver_id, tx: r.transaction_hash });
      }
    }
    cursor = d?.cursor;
    if (!cursor || (d?.txns || []).length < 25) break;
    await sleep(1200);
  }
  return out.sort((a, b) => a.at - b.at);
}

/** Give each delivered hire its settlement. A transfer is used once; no match leaves the row untouched. */
export function match(rows, sent, { slackMs = 15 * 60 * 1000 } = {}) {
  const used = new Set();
  let matched = 0;
  for (const row of [...rows].sort((a, b) => a.at.localeCompare(b.at))) {
    if (!row.delivered || !row.pay_to) continue;
    const from = Date.parse(row.at);
    const amount = String(Math.round(Number(row.price_usdc) * 1e6));
    const hit = sent.find((t) => !used.has(t.tx) && t.to === row.pay_to && t.amount === amount && t.at >= from && t.at <= from + (row.response?.ms || 0) + slackMs);
    if (!hit) continue;
    used.add(hit.tx);
    row.settle_tx = hit.tx;
    matched += 1;
  }
  return matched;
}

if (fileURLToPath(import.meta.url) === process.argv[1]) {
  const logPath = `${here}docs/hires.json`;
  const log = JSON.parse(readFileSync(logPath, 'utf8'));
  const dir = `${here}receipts`;
  const files = existsSync(dir) ? readdirSync(dir).filter((n) => n.endsWith('.json')).map((n) => `${dir}/${n}`) : [];
  const receipts = files.map((f) => [f, JSON.parse(readFileSync(f, 'utf8'))]).filter(([, r]) => r.market === 'near-agent-market');
  const rows = [...log.filter((r) => !r.void), ...receipts.map(([, r]) => r)];
  const payer = rows.find((r) => r.payer)?.payer;
  const sent = await transfers(payer);
  const matched = match(rows, sent);
  writeFileSync(logPath, JSON.stringify(log, null, 1));
  for (const [f, r] of receipts) if (r.settle_tx) writeFileSync(f, JSON.stringify(r, null, 2));
  const treasury = rows.find((r) => r.pay_to)?.pay_to;
  const total = sent.filter((t) => t.to === treasury).reduce((s, t) => s + Number(t.amount), 0) / 1e6;
  console.log(JSON.stringify({ delivered: rows.filter((r) => r.delivered).length, matched, settled_to_marketplace_usdc: total.toFixed(2), transfers_to_marketplace: sent.filter((t) => t.to === treasury).length }));
}
