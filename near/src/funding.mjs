// What it actually took to fund a Base payment from NEAR, read back from 1Click.
// Deposit times come from our own run log; everything else is re-queried live, so
// anyone can check a row by its deposit address.

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';

const here = new URL('..', import.meta.url).pathname;
export const ONECLICK = 'https://1click.chaindefuser.com/v0';

/** Deposit address -> when we sent the deposit. Receipts carry most of them; the
 *  first attempt ran before receipts recorded the address, so it is named here. */
export function attempts(root = here) {
  // Both deposits predate the receipt that records an address, and the wallet they
  // funded was later reused, so a receipt's own timestamp is the wrong end of the wait.
  const seeded = {
    '8a48c15e5ce84d6b60a16e5dd5f966fbdb970e3bd1ec5adc42497fe76f2893de': '2026-11-12T07:42:00Z',
    '40085f1c4aea515350b5e58fdec68badd751690441cd0884e806218b7293ad42': '2026-11-12T10:22:00Z',
  };
  const dir = `${root}receipts`;
  if (existsSync(dir)) {
    for (const f of readdirSync(dir).filter((n) => n.endsWith('.json'))) {
      const r = JSON.parse(readFileSync(`${dir}/${f}`, 'utf8'));
      const a = r?.funding?.deposit_address;
      if (a && !seeded[a]) seeded[a] = r.at;
    }
  }
  return seeded;
}

export function row(address, sentAt, status) {
  const d = status?.swapDetails || {};
  const endedAt = status?.updatedAt ?? null;
  return {
    address,
    sent_at: sentAt,
    ended_at: endedAt,
    // 1Click reports its last record update, which can trail the refund we saw, so
    // this is the outer bound of the wait rather than an exact duration.
    seconds: endedAt && sentAt ? Math.max(0, Math.round((Date.parse(endedAt) - Date.parse(sentAt)) / 1000)) : null,
    status: status?.status ?? 'UNKNOWN',
    sent: d.amountInFormatted ?? d.depositedAmountFormatted ?? null,
    received: d.amountOutFormatted ?? null,
    reason: d.refundReason ?? null,
  };
}

export async function funding(root = here, { fetchImpl = globalThis.fetch } = {}) {
  const out = [];
  for (const [address, sentAt] of Object.entries(attempts(root))) {
    const status = await fetchImpl(`${ONECLICK}/status?depositAddress=${address}`).then((r) => r.json()).catch(() => null);
    out.push(row(address, sentAt, status));
  }
  return out.sort((a, b) => String(a.sent_at).localeCompare(String(b.sent_at)));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const rows = await funding();
  mkdirSync(`${here}docs`, { recursive: true });
  writeFileSync(`${here}docs/funding.json`, JSON.stringify(rows, null, 1));
  console.log(JSON.stringify({ attempts: rows.length, succeeded: rows.filter((r) => r.status === 'SUCCESS').length }));
}
