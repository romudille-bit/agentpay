import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buy } from '../src/buy.mjs';
import { NEAR_USDC } from '../src/challenge.mjs';
import { OverCap, cappedFetch, deliveredFrom } from '../src/pay.mjs';

const ok = { status: 200, ms: 5, delivered: true, body_bytes: 10, body_sha256: 'x', error_body: null, settle: { success: true, transaction: 'tx' } };
const base = { accountId: 'me.near', privateKey: 'ed25519:x' };
const checked = (over) => async () => ({ verdict: 'pass', reason: 'ok', rail: 'near', price_usdc: '0.01', pay_to: 'seller.near', ...over });
const never = (name) => async () => { throw new Error(`${name} must not be called`); };

test('delivered means 2xx with a real body', () => {
  assert.equal(deliveredFrom(200, '{"price":1}'), true);
  assert.equal(deliveredFrom(200, ''), false);
  assert.equal(deliveredFrom(200, '{"error":"nope"}'), false);
  assert.equal(deliveredFrom(402, '{"price":1}'), false);
});

test('a blocked seller is refused before any payment', async () => {
  const r = await buy('https://bad.example', { ...base, deps: { check: checked({ verdict: 'block' }), payNear: never('payNear'), fundBaseWallet: never('fund') } });
  assert.match(r.refused, /flagged/);
  assert.equal(r.paid, false);
});

test('a price over the cap is refused before any payment', async () => {
  const r = await buy('https://dear.example', { ...base, maxUsdc: '0.005', deps: { check: checked({}), payNear: never('payNear') } });
  assert.match(r.refused, /over the cap/);
});

test('no payable rail is refused', async () => {
  const r = await buy('https://sol.example', { ...base, deps: { check: checked({ rail: null, price_usdc: null }) } });
  assert.match(r.refused, /no rail/);
});

test('NEAR rail pays directly and records delivery', async () => {
  const r = await buy('https://near.example', { ...base, deps: { check: checked({}), payNear: async () => ok, fundBaseWallet: never('fund') } });
  assert.equal(r.paid, true);
  assert.equal(r.delivered, true);
  assert.equal(r.funding, undefined);
});

test('Base rail reuses a session wallet that holds enough', async () => {
  let key;
  const r = await buy('https://base.example', { ...base, deps: {
    check: checked({ rail: 'base-via-intents', price_usdc: '0.005' }),
    sessionWallets: async () => [{ address: '0xlow', privateKey: '0x1', usdc_atomic: 100n }, { address: '0xfull', privateKey: '0x2', usdc_atomic: 250000n }],
    fundBaseWallet: never('fund'),
    payBase: async (_u, o) => { key = o.evmKey; return ok; },
  } });
  assert.equal(key, '0x2');
  assert.equal(r.funding.reused, true);
  assert.equal(r.paid, true);
});

test('Base rail with a swap still in flight does not pay and never leaks the key', async () => {
  const r = await buy('https://base.example', { ...base, deps: {
    check: checked({ rail: 'base-via-intents', price_usdc: '0.005' }),
    sessionWallets: async () => [],
    fundBaseWallet: async () => ({ evmKey: '0xsecret', address: '0xnew', ok: false, intents_status: 'PENDING' }),
    payBase: never('payBase'),
  } });
  assert.match(r.refused, /still in flight/);
  assert.equal(JSON.stringify(r).includes('0xsecret'), false);
});

const quote402 = (accept) => ({ status: 402, headers: { get: () => null }, clone: () => ({ json: async () => ({ accepts: [accept] }) }) });
const nearAccept = { scheme: 'exact', network: 'near:mainnet', asset: NEAR_USDC, amount: '10000', payTo: 'seller.near' };

test('the cap is checked on the quote that would be signed, not on the earlier check', async () => {
  const dear = cappedFetch(async () => quote402({ ...nearAccept, amount: '5000000' }), '0.02');
  await assert.rejects(dear('https://x.example'), OverCap);
  const other = cappedFetch(async () => quote402({ ...nearAccept, asset: 'wbtc.near' }), '0.02');
  await assert.rejects(other('https://x.example'), /not USDC/);
  const fair = cappedFetch(async () => quote402(nearAccept), '0.02');
  assert.equal((await fair('https://x.example')).status, 402);
  const paid = cappedFetch(async () => ({ status: 200 }), '0.02');
  assert.equal((await paid('https://x.example')).status, 200);
});

test('a seller that raises its price at payment time is refused, not paid', async () => {
  const r = await buy('https://bait.example', { ...base, deps: { check: checked({}), payNear: async () => { throw new OverCap('seller asked for 5000000 units at payment time, over the cap 0.02'); } } });
  assert.match(r.refused, /at payment time/);
  assert.equal(r.paid, false);
  assert.equal(r.delivered, false);
});
