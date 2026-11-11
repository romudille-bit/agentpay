#!/usr/bin/env node
import { check } from '../src/check.mjs';
import { createOneClick, overhead } from '../src/oneclick.mjs';

const [cmd, arg, ...rest] = process.argv.slice(2);
const flag = (name) => { const i = rest.indexOf(`--${name}`); return i >= 0 ? rest[i + 1] : undefined; };

if (cmd === 'check' && arg) {
  const body = flag('body') ? JSON.parse(flag('body')) : undefined;
  const r = await check(arg, { method: flag('method') || (body ? 'POST' : 'GET'), body });
  console.log(JSON.stringify(r, null, 2));
  process.exit(r.verdict === 'block' ? 2 : 0);
} else if (cmd === 'quote' && arg) {
  const q = await createOneClick().quoteUsdcOnBase({
    usdcOut: arg, recipient: flag('to') || '0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7', refundTo: flag('refund') || 'near', dry: true,
  });
  console.log(JSON.stringify({ usdc_on_base: arg, cost_near: q.amountInFormatted, cost_usd: q.amountInUsd, overhead: overhead(q), eta_s: q.timeEstimate }, null, 2));
} else {
  console.error('usage: near-check check <url> [--method POST] [--body \'{"k":"v"}\']\n       near-check quote <usdc-amount>');
  process.exit(64);
}
