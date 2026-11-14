import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LABEL, outcome, summarise } from '../src/page.mjs';

const hour = 3600 * 1000;
const row = (over = {}) => ({ at: new Date(Date.now() - hour).toISOString(), handle: 'a', price_usdc: '0.01', window_s: 600, delivered: false, charged: false, response: { state: 'TASK_STATE_COMPLETED', ms: 1000, artifact_chars: 10 }, ...over });

test('every outcome has a label', () => {
  for (const k of ['delivered', 'rejected', 'no_start', 'cut_short', 'no_answer', 'empty', 'unavailable', 'working']) assert.ok(LABEL[k]);
});

test('a completed hire with an artifact is delivered', () => {
  assert.equal(outcome(row({ delivered: true })), 'delivered');
});

test('a completed hire with nothing attached is not delivered', () => {
  assert.equal(outcome(row({ response: { state: 'TASK_STATE_COMPLETED', artifact_chars: 0 } })), 'empty');
});

test('rejected, cancelled and unreachable are told apart', () => {
  assert.equal(outcome(row({ response: { state: 'TASK_STATE_REJECTED' } })), 'rejected');
  assert.equal(outcome(row({ response: { state: 'TASK_STATE_CANCELED' } })), 'no_start');
  assert.equal(outcome(row({ refused: 'no reachable backend' })), 'unavailable');
});

test('a task still open inside its window is working, and past it is no answer', () => {
  assert.equal(outcome(row({ at: new Date().toISOString(), response: { state: 'TASK_STATE_WORKING' } })), 'working');
  assert.equal(outcome(row({ response: { state: 'TASK_STATE_WORKING' } })), 'no_answer');
});

test('the rate counts settled hires only, and the total is what settled', () => {
  const s = summarise([
    row({ delivered: true, charged: true }),
    row({ response: { state: 'TASK_STATE_CANCELED' } }),
    row({ at: new Date().toISOString(), response: { state: 'TASK_STATE_WORKING' } }),
  ]);
  assert.equal(s.hired, 3);
  assert.equal(s.settled, 2);
  assert.equal(s.delivered, 1);
  assert.equal(s.rate, 50);
  assert.equal(s.charged, '0.01');
  assert.equal(s.quoted, '0.03');
});

test('an empty log does not divide by zero', () => {
  assert.deepEqual(summarise([]).rate, 0);
});

test('an agent hired twice keeps only its last try', async () => {
  const { mkdtempSync, mkdirSync, writeFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { hires } = await import('../src/page.mjs');
  const root = `${mkdtempSync(`${tmpdir()}/near-`)}/`;
  mkdirSync(`${root}docs`); mkdirSync(`${root}receipts`);
  writeFileSync(`${root}docs/hires.json`, JSON.stringify([]));
  const one = (at, delivered) => JSON.stringify({ at, market: 'near-agent-market', handle: 'x', delivered, response: { state: delivered ? 'TASK_STATE_COMPLETED' : null } });
  writeFileSync(`${root}receipts/a.json`, one('2026-11-12T11:35:20Z', false));
  writeFileSync(`${root}receipts/b.json`, one('2026-11-12T11:35:34Z', true));
  const rows = hires(root);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].delivered, true);
});

test('a silent agent costs its whole window; a delivered one costs its run', async () => {
  const { spent } = await import('../src/page.mjs');
  assert.equal(spent(row({ delivered: true, response: { state: 'TASK_STATE_COMPLETED', ms: 30000, artifact_chars: 5 } })), 30);
  assert.equal(spent(row({ window_s: 1200, response: { state: 'TASK_STATE_WORKING' } })), 1200);
  assert.equal(spent(row({ response: { state: 'TASK_STATE_CANCELED', ms: 120000 } })), 120);
  assert.equal(spent(row({ refused: 'no backend' })), 0);
});

test('the summary separates time waited from time that produced work', async () => {
  const s = summarise([
    row({ delivered: true, charged: true, response: { state: 'TASK_STATE_COMPLETED', ms: 60000, artifact_chars: 9 } }),
    row({ at: new Date(Date.now() - 2 * hour).toISOString(), window_s: 3600, response: { state: 'TASK_STATE_WORKING' } }),
  ]);
  assert.equal(s.failed, 1);
  assert.equal(s.fail_rate, 50);
  assert.equal(s.waited_h, 1);
  assert.equal(s.worked_min, 1);
});

test('funding rows carry the measured wait, the unit sent and the refund reason', async () => {
  const { row: frow } = await import('../src/funding.mjs');
  const usdc = { quoteRequest: { originAsset: 'nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1' } };
  const r = frow('abc', { status: 'SUCCESS', quoteResponse: usdc, swapDetails: { amountInFormatted: '0.3', amountOutFormatted: '0.297538' } }, { sentAt: '2026-11-12T10:22:00Z', endedAt: '2026-11-12T12:42:00Z' });
  assert.equal(r.seconds, 8400);
  assert.equal(r.received, '0.297538');
  assert.equal(r.sent_unit, 'USDC');
  const bad = frow('def', { status: 'REFUNDED', quoteResponse: { quoteRequest: { originAsset: 'nep141:wrap.near' } }, swapDetails: { refundReason: 'QUOTE_EXPIRED', depositedAmountFormatted: '0.075' } });
  assert.equal(bad.reason, 'QUOTE_EXPIRED');
  assert.equal(bad.sent_unit, 'NEAR');
  assert.equal(bad.received, null);
  assert.equal(bad.seconds, null);
});

test('each delivered hire gets one settlement, and a transfer is never used twice', async () => {
  const { match } = await import('../src/settlements.mjs');
  const at = '2026-11-13T10:44:27.000Z';
  const t0 = Date.parse(at);
  const hire = (over) => ({ at, handle: 'h', delivered: true, price_usdc: '0.01', pay_to: 'treasury', response: { ms: 50000 }, ...over });
  const rows = [hire({}), hire({ at: '2026-11-13T10:45:26.000Z' }), hire({ delivered: false }), hire({ price_usdc: '0.04', at: '2026-11-13T11:27:59.000Z' })];
  const sent = [
    { at: t0 - 60000, amount: '10000', to: 'treasury', tx: 'earlier' },
    { at: t0 + 52000, amount: '10000', to: 'treasury', tx: 'A' },
    { at: t0 + 92000, amount: '10000', to: 'treasury', tx: 'B' },
    { at: t0 + 60000, amount: '10000', to: 'someone-else', tx: 'C' },
  ];
  assert.equal(match(rows, sent), 2);
  assert.deepEqual(rows.map((r) => r.settle_tx), ['A', 'B', undefined, undefined]);
});

test('hires we cut short and agents never reached stay out of the count', () => {
  const s = summarise([
    row({ delivered: true, charged: true }),
    row({ response: { state: 'TASK_STATE_WORKING' } }),
    row({ sla_s: 600, response: { state: 'TASK_STATE_CANCELED', ms: 122000 } }),
    row({ sla_s: 120, response: { state: 'TASK_STATE_CANCELED', ms: 442000 } }),
    row({ refused: 'no reachable backend', price_usdc: undefined }),
  ]);
  assert.equal(outcome(row({ sla_s: 600, response: { state: 'TASK_STATE_CANCELED', ms: 122000 } })), 'cut_short');
  assert.equal(s.hired, 4);
  assert.equal(s.settled, 3);
  assert.equal(s.cut_short, 1);
  assert.equal(s.unreachable, 1);
  assert.equal(s.failed, 2);
  assert.equal(s.fail_rate, 67);
});
