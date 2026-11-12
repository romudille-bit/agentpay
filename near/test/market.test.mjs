import { test } from 'node:test';
import assert from 'node:assert/strict';
import { artifactText, hire, hireDelivered } from '../src/market.mjs';

const row = { scheme: 'exact', network: 'near:mainnet', asset: 'usdc.near', amount: '10000', payTo: 'treasury', maxTimeoutSeconds: 900 };
const res = (status, body) => ({ status, json: async () => body });
const done = (text) => ({ id: 't1', status: { state: 'TASK_STATE_COMPLETED' }, artifacts: text === null ? [] : [{ parts: [{ text }] }] });
const base = { accountId: 'me.near', privateKey: 'k', sign: async () => 'HEADER', sleep: async () => {} };

/** Fake market: first POST quotes, the paid POST returns `paid`, GetTask returns `polled` in turn. */
function market({ quote = res(402, { accepts: [row] }), paid, polled = [] }) {
  const calls = [];
  const fetchImpl = async (_url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ method: body.method, payment: init.headers['X-PAYMENT'] ?? null, config: body.params?.configuration });
    if (body.method === 'GetTask') return res(200, { result: polled.shift() });
    return init.headers['X-PAYMENT'] ? paid : quote;
  };
  return { fetchImpl, calls };
}

test('delivered needs a completed task with a non-empty artifact', () => {
  assert.equal(hireDelivered(done('hello')), true);
  assert.equal(hireDelivered(done('   ')), false);
  assert.equal(hireDelivered(done(null)), false);
  assert.equal(hireDelivered({ status: { state: 'TASK_STATE_FAILED' }, artifacts: [{ parts: [{ text: 'x' }] }] }), false);
  assert.equal(artifactText({ artifacts: [{ parts: [{ url: 'https://f/x.pdf' }] }] }), 'https://f/x.pdf');
});

test('a price over the cap is refused before anything is signed', async () => {
  const m = market({ quote: res(402, { accepts: [{ ...row, amount: '3000000' }] }) });
  const r = await hire('dear', 'task', { ...base, maxUsdc: '0.10', fetchImpl: m.fetchImpl, sign: async () => { throw new Error('must not sign'); } });
  assert.match(r.refused, /over the cap/);
  assert.equal(m.calls.length, 1);
});

test('an agent that is not taking calls is reported with the market reason', async () => {
  const m = market({ quote: res(200, { error: { code: -32603, message: 'no reachable backend' } }) });
  const r = await hire('down', 'task', { ...base, fetchImpl: m.fetchImpl });
  assert.equal(r.refused, 'no reachable backend');
  assert.equal(r.paid, false);
});

test('a hire that completes in the call is delivered and charged', async () => {
  const m = market({ paid: res(200, { result: { task: done('the answer') } }) });
  const r = await hire('quick', 'task', { ...base, fetchImpl: m.fetchImpl });
  assert.equal(r.delivered, true);
  assert.equal(r.charged, true);
  assert.equal(r.price_usdc, '0.01');
  assert.equal(m.calls[1].payment, 'HEADER');
  assert.equal(r.response.artifact_chars, 10);
});

test('a working task is polled until it ends; a rejected one is not charged', async () => {
  const working = { id: 't1', status: { state: 'TASK_STATE_WORKING' } };
  const m = market({ paid: res(200, { result: { task: working } }), polled: [working, { id: 't1', status: { state: 'TASK_STATE_REJECTED' } }] });
  const r = await hire('slow', 'task', { ...base, fetchImpl: m.fetchImpl });
  assert.equal(r.response.state, 'TASK_STATE_REJECTED');
  assert.equal(r.delivered, false);
  assert.equal(r.charged, false);
  assert.equal(m.calls.filter((c) => c.method === 'GetTask').length, 2);
});

test('a rejected payment is not a hire', async () => {
  const m = market({ paid: res(402, { error: 'invalid payment' }) });
  const r = await hire('strict', 'task', { ...base, fetchImpl: m.fetchImpl });
  assert.equal(r.paid, false);
  assert.equal(r.response.status, 402);
});

test('immediate mode asks the market to return the task id at once', async () => {
  const m = market({ paid: res(200, { result: { task: { id: 't1', status: { state: 'TASK_STATE_SUBMITTED' } } } }) });
  const r = await hire('later', 'task', { ...base, immediate: true, waitMs: 0, fetchImpl: m.fetchImpl });
  assert.equal(m.calls[1].config.returnImmediately, true);
  assert.equal(r.response.task_id, 't1');
  assert.equal(r.paid, true);
});
