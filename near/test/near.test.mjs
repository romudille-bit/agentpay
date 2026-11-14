import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toUnits, overhead, createOneClick, ASSETS } from '../src/oneclick.mjs';
import { BASE_USDC, NEAR_USDC, decodePaymentRequired, offers, pickRail, readChallenge } from '../src/challenge.mjs';
import { check, fromUnits } from '../src/check.mjs';

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64');
const res = (status, { headers = {}, json } = {}) => ({
  status, ok: status < 400,
  headers: { get: (k) => headers[k.toLowerCase()] ?? null },
  json: async () => json, text: async () => JSON.stringify(json ?? {}),
});
const nearOffer = { scheme: 'exact', network: 'near:mainnet', amount: '10000', asset: NEAR_USDC, payTo: 'seller.near' };
const baseOffer = { scheme: 'exact', network: 'eip155:8453', amount: '10000', asset: BASE_USDC, payTo: '0xabc' };

test('toUnits is exact and rejects bad input', () => {
  assert.equal(toUnits('0.01', 6), '10000');
  assert.equal(toUnits('1', 24), '1000000000000000000000000');
  assert.throws(() => toUnits('0.0000001', 6));
  assert.throws(() => toUnits('-1', 6));
});

test('fromUnits round-trips prices', () => {
  assert.equal(fromUnits('10000'), '0.01');
  assert.equal(fromUnits('1000000'), '1');
  assert.equal(fromUnits('1'), '0.000001');
});

test('overhead is cost over delivered value', () => {
  assert.ok(Math.abs(overhead({ amountInUsd: '1.0127', amountOutUsd: '0.9996' }) - 0.0131) < 0.001);
  assert.equal(overhead({}), null);
});

test('quote asks for exact USDC out on Base and never commits when dry', async () => {
  let sent;
  const oc = createOneClick({ fetchImpl: async (url, init) => { sent = { url, body: JSON.parse(init.body) }; return res(200, { json: { quote: { amountInFormatted: '0.27' } } }); } });
  const q = await oc.quoteUsdcOnBase({ usdcOut: '1', recipient: '0xabc', refundTo: 'me.near', now: 0 });
  assert.equal(q.amountInFormatted, '0.27');
  assert.match(sent.url, /\/quote$/);
  assert.equal(sent.body.dry, true);
  assert.equal(sent.body.swapType, 'EXACT_OUTPUT');
  assert.equal(sent.body.amount, '1000000');
  assert.equal(sent.body.destinationAsset, ASSETS.USDC_BASE.id);
  assert.equal(sent.body.refundTo, 'me.near');
});

test('1click errors carry the status', async () => {
  const oc = createOneClick({ fetchImpl: async () => res(400, { json: { message: 'bad' } }) });
  await assert.rejects(() => oc.status('x'), /1click .* 400/);
});

test('decodes a v2 header and ignores junk', () => {
  assert.deepEqual(decodePaymentRequired(b64({ accepts: [nearOffer] })).accepts[0], nearOffer);
  assert.equal(decodePaymentRequired('%%%'), null);
  assert.equal(decodePaymentRequired(null), null);
});

test('offers normalises v1 names and drops non-exact', () => {
  const list = offers({ accepts: [{ scheme: 'exact', network: 'base', maxAmountRequired: '5000', asset: BASE_USDC, payTo: '0x1' }, { scheme: 'upto', network: 'near:mainnet', amount: '1' }] });
  assert.equal(list.length, 1);
  assert.equal(list[0].network, 'eip155:8453');
  assert.equal(list[0].amount, '5000');
});

test('rail: NEAR direct wins, Base goes through Intents, else none', () => {
  assert.equal(pickRail({ accepts: [baseOffer, nearOffer] }).rail, 'near');
  assert.equal(pickRail({ accepts: [baseOffer] }).rail, 'base-via-intents');
  assert.equal(pickRail({ accepts: [{ ...baseOffer, network: 'solana:x' }] }).rail, null);
  assert.equal(pickRail(null).rail, null);
});

test('readChallenge falls back to a v1 body', async () => {
  const r = await readChallenge('https://s.example/x', { fetchImpl: async () => res(402, { json: { accepts: [baseOffer] } }) });
  assert.equal(r.status, 402);
  assert.equal(r.challenge.accepts.length, 1);
});

test('check: a blocked seller is never paid, even with a payable rail', async () => {
  const r = await check('https://bad.example/api', {
    guard: async () => ({ verdict: 'block', reason: 'took payment without delivering' }),
    fetchImpl: async () => res(402, { headers: { 'payment-required': b64({ accepts: [nearOffer] }) } }),
  });
  assert.equal(r.verdict, 'block');
  assert.equal(r.rail, 'near');
  assert.equal(r.pay, false);
});

test('check: a Base seller gets a NEAR funding quote', async () => {
  const r = await check('https://ok.example/api', {
    guard: async () => ({ verdict: 'pass', reason: '100% delivered' }),
    fetchImpl: async () => res(402, { headers: { 'payment-required': b64({ accepts: [baseOffer] }) } }),
    oneClick: { quoteUsdcOnBase: async ({ usdcOut, dry }) => { assert.equal(usdcOut, '0.01'); assert.equal(dry, true); return { amountInFormatted: '0.0033', amountInUsd: '0.0125', amountOutUsd: '0.01', timeEstimate: 24 }; } },
  });
  assert.equal(r.rail, 'base-via-intents');
  assert.equal(r.price_usdc, '0.01');
  assert.equal(r.funding.cost_near, '0.0033');
  assert.equal(r.pay, true);
});

test('check: an unreachable seller reports the error and does not pay', async () => {
  const r = await check('https://down.example/api', {
    guard: async () => ({ verdict: 'unknown', reason: 'not in scores.json' }),
    fetchImpl: async () => { throw new Error('ECONNREFUSED'); },
  });
  assert.equal(r.unpaid.error, 'ECONNREFUSED');
  assert.equal(r.pay, false);
});

test('offers in any token other than USDC are dropped', () => {
  assert.equal(offers({ accepts: [{ ...nearOffer, asset: 'wbtc.near' }] }).length, 0);
  assert.equal(offers({ accepts: [{ ...baseOffer, asset: '0x0000000000000000000000000000000000000001' }] }).length, 0);
  assert.equal(offers({ accepts: [{ ...baseOffer, asset: BASE_USDC.toLowerCase() }] }).length, 1);
  assert.equal(pickRail({ accepts: [{ ...nearOffer, asset: 'wbtc.near' }, baseOffer] }).rail, 'base-via-intents');
});
