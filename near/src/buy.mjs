// check -> pay on the right rail -> verify delivery -> receipt.

import { mkdirSync, writeFileSync } from 'node:fs';
import { check } from './check.mjs';
import { toUnits } from './oneclick.mjs';
import { OverCap, fundBaseWallet, payBase, payNear, sessionWallets } from './pay.mjs';

const gt = (a, b) => BigInt(toUnits(a, 6)) > BigInt(toUnits(b, 6));

/** `maxUsdc` caps the price of this call; `fundUsdc` is what a Base wallet is topped up with. */
export async function buy(url, { accountId, privateKey, maxUsdc = '0.02', fundUsdc = '0.10', method = 'GET', body, receiptsDir = null, walletDir = null, log = () => {}, deps = {} } = {}) {
  const doCheck = deps.check || check;
  const receipt = { at: new Date().toISOString(), url, payer: accountId, max_usdc: String(maxUsdc), paid: false, refused: null };
  const c = await doCheck(url, { method, body, nearAccount: accountId });
  Object.assign(receipt, { verdict: c.verdict, reason: c.reason, rail: c.rail, price_usdc: c.price_usdc, pay_to: c.pay_to });

  if (c.verdict === 'block') receipt.refused = 'seller is flagged for not delivering';
  else if (!c.rail) receipt.refused = 'no rail a NEAR account can pay on';
  else if (gt(c.price_usdc, maxUsdc)) receipt.refused = `price ${c.price_usdc} is over the cap ${maxUsdc}`;

  if (!receipt.refused) {
    if (c.rail === 'near') {
      try {
        receipt.response = await (deps.payNear || payNear)(url, { accountId, privateKey, maxUsdc, method, body });
      } catch (e) {
        if (!(e instanceof OverCap)) throw e;
        receipt.refused = e.message; // the paying request was quoted differently from the check
      }
    } else {
      // Reuse a session wallet that still holds enough; bridge only when none does.
      const price = BigInt(toUnits(c.price_usdc, 6));
      const held = (await (deps.sessionWallets || sessionWallets)(walletDir)).find((w) => w.usdc_atomic >= price);
      let evmKey = held?.privateKey;
      if (held) {
        receipt.funding = { reused: true, address: held.address, deposit_address: held.deposit_address ?? null, deposit_tx: held.deposit_tx ?? null, cost_near: held.cost_near ?? null };
      } else {
        const fund = gt(c.price_usdc, fundUsdc) ? c.price_usdc : fundUsdc;
        const f = await (deps.fundBaseWallet || fundBaseWallet)({ accountId, privateKey, usdc: fund, walletDir, log });
        ({ evmKey, ...receipt.funding } = f);
        if (!f.ok) {
          receipt.refused = f.intents_status === 'PENDING' ? 'funding swap still in flight; run again to pay from the same wallet' : `funding did not complete (${f.intents_status})`;
        }
      }
      if (!receipt.refused) receipt.response = await (deps.payBase || payBase)(url, { evmKey, maxUsdc, method, body });
    }
    receipt.paid = Boolean(receipt.response && receipt.response.status !== 402 && receipt.response.settle);
    receipt.delivered = receipt.response?.delivered ?? false;
  }
  if (receiptsDir) {
    mkdirSync(receiptsDir, { recursive: true });
    receipt.file = `receipts/${receipt.at.replace(/[:.]/g, '-')}.json`;
    writeFileSync(`${receiptsDir}/${receipt.file.slice(9)}`, JSON.stringify(receipt, null, 2));
  }
  return receipt;
}
