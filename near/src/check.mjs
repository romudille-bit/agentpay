// The pre-payment check: delivery verdict, the seller's live 402, the rail a
// NEAR account would pay on, and what funding that payment costs in NEAR.

import { checkUrl } from '@romudille/x402-guard';
import { readChallenge, pickRail } from './challenge.mjs';
import { createOneClick, overhead } from './oneclick.mjs';

const USDC_DECIMALS = 6;
export function fromUnits(units, decimals = USDC_DECIMALS) {
  const s = String(units).padStart(decimals + 1, '0');
  return `${s.slice(0, -decimals)}.${s.slice(-decimals)}`.replace(/\.?0+$/, '') || '0';
}

export async function check(url, { method = 'GET', body, fetchImpl = globalThis.fetch, guard = checkUrl, oneClick = createOneClick({ fetchImpl }), nearAccount = 'near' } = {}) {
  const [verdict, unpaid] = await Promise.all([
    guard(url),
    readChallenge(url, { method, body, fetchImpl }).catch((e) => ({ status: null, latencyMs: null, challenge: null, error: e.message })),
  ]);
  const { rail, offer } = pickRail(unpaid.challenge);
  const out = {
    url,
    verdict: verdict.verdict,
    reason: verdict.reason,
    alternative: verdict.alternative || null,
    unpaid: { status: unpaid.status, latency_ms: unpaid.latencyMs, error: unpaid.error || null },
    rail,
    price_usdc: offer ? fromUnits(offer.amount) : null,
    pay_to: offer?.payTo || null,
    funding: null,
    pay: verdict.verdict !== 'block' && rail !== null,
  };
  if (rail === 'base-via-intents') {
    try {
      // Dry quote: the recipient only shapes the price, nothing is sent.
      const q = await oneClick.quoteUsdcOnBase({ usdcOut: out.price_usdc, recipient: offer.payTo, refundTo: nearAccount, dry: true });
      out.funding = { from: 'wNEAR', cost_near: q.amountInFormatted, overhead: overhead(q), eta_s: q.timeEstimate };
    } catch (e) {
      out.funding = { error: e.message };
    }
  } else if (rail === 'near') {
    out.funding = { from: 'USDC on NEAR', cost_near: null, overhead: 0, eta_s: null };
  }
  return out;
}
