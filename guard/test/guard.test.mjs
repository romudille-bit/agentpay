import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  normalizeUrl, decide, ScoreIndex, createScoreSource, checkUrl, deliveryGuard,
} from '../src/index.mjs';

const fixture = JSON.parse(await readFile(new URL('./fixtures/scores.json', import.meta.url), 'utf8'));
const byName = Object.fromEntries(fixture.services.map((s) => [s.name, s]));

function fakeFetch(body, { status = 200 } = {}) {
  const calls = { n: 0 };
  const fn = async () => { calls.n += 1; return { ok: status < 400, status, json: async () => body }; };
  return { fn, calls };
}

test('normalizeUrl: lowercases host, drops query/fragment/trailing slash, rejects junk', () => {
  assert.equal(normalizeUrl('HTTPS://Good.Example/v1/search/?q=1#x'), 'https://good.example/v1/search');
  assert.equal(normalizeUrl('https://good.example'), 'https://good.example');
  assert.equal(normalizeUrl('ftp://x'), null);
  assert.equal(normalizeUrl(''), null);
  assert.equal(normalizeUrl(undefined), null);
});

test('decide: verdict matrix', () => {
  assert.equal(decide(byName['Good Search']).verdict, 'pass');
  assert.equal(decide(byName['New Price']).verdict, 'warn');           // provisional
  assert.equal(decide(byName['Thief Inference']).verdict, 'block');    // flagged, rate 0.5
  assert.equal(decide(byName['Recovered Screen']).verdict, 'warn');    // flagged once, delivered since
  assert.equal(decide(byName['Latest Fail News']).verdict, 'block');   // flagged, latest probe failed
  assert.equal(decide(byName['Unprobed OCR']).verdict, 'unknown');
  assert.equal(decide(null).verdict, 'unknown');
});

test('decide: thresholds are configurable', () => {
  const strict = { block: 0.5, pass: 0.99 };
  assert.equal(decide({ ...byName['Good Search'], delivery_rate: 0.95 }, strict).verdict, 'warn');
});

test('ScoreIndex: exact and prefix matching', () => {
  const idx = new ScoreIndex(fixture);
  assert.equal(idx.size, 7);
  assert.equal(idx.lookup('https://good.example/v1/search?x=1').match, 'exact');
  assert.equal(idx.lookup('https://good.example/v1/search/extra').match, 'prefix');
  assert.equal(idx.lookup('https://good.example/v1/searchextra'), null);   // not a path boundary
  assert.equal(idx.lookup('https://other.example/v1/search'), null);
  assert.equal(idx.lookup('not a url'), null);
});

test('createScoreSource: caches within ttl, fails open on error, keeps last good index', async () => {
  const ok = fakeFetch(fixture);
  const src = createScoreSource({ fetchImpl: ok.fn, ttlMs: 60_000 });
  await src.get(); await src.get();
  assert.equal(ok.calls.n, 1);

  const bad = fakeFetch(null, { status: 500 });
  const src2 = createScoreSource({ fetchImpl: bad.fn, ttlMs: 60_000 });
  const idx = await src2.get();
  assert.equal(idx.size, 0);
  assert.match(src2.lastError.message, /500/);

  let flip = false;
  const flaky = async () => (flip = !flip) ? { ok: true, status: 200, json: async () => fixture }
                                           : { ok: false, status: 503, json: async () => null };
  const src3 = createScoreSource({ fetchImpl: flaky, ttlMs: 0 });
  assert.equal((await src3.get()).size, 7);
  assert.equal((await src3.get()).size, 7);   // 503 → previous index kept
});

test('ScoreIndex.alternatives: passing providers for the same need, excluding self', () => {
  const idx = new ScoreIndex(fixture);
  const alts = idx.alternatives(byName['Thief Inference']);
  assert.deepEqual(alts.map((e) => e.name), ['Honest Inference']);
  assert.deepEqual(idx.alternatives(byName['Honest Inference']), []);   // thief does not pass
  assert.deepEqual(idx.alternatives(byName['Unprobed OCR']), []);
  assert.deepEqual(idx.alternatives(null), []);
});

test('checkUrl: block/warn carry a verified_route pointer when alternatives exist', async () => {
  const source = createScoreSource({ fetchImpl: fakeFetch(fixture).fn });
  const blocked = await checkUrl('https://thief.example/v1/infer', { source });
  assert.equal(blocked.alternative.need, 'llm inference');
  assert.equal(blocked.alternative.scored_providers, 1);
  assert.match(blocked.alternative.verified_route, /verified_route/);
  assert.match(blocked.reason, /1 scored provider deliver for "llm inference"; ask verified_route/);
  const warned = await checkUrl('https://new.example/api/price', { source });   // only token-price entry
  assert.equal(warned.alternative, null);
  const passed = await checkUrl('https://honest.example/v1/infer', { source });
  assert.equal(passed.alternative, null);
});

test('deliveryGuard: sessionBudget aborts past the cap in every mode, ledger and spent track commitments', async () => {
  const hook = deliveryGuard({
    mode: 'warn', sessionBudget: { maxTotalUsd: 0.025 }, fetchImpl: fakeFetch(fixture).fn, logger: () => {},
  });
  const ctx = (url, amount) => ({ paymentRequired: { resource: { url }, accepts: [] }, selectedRequirements: { network: 'eip155:8453', amount } });

  assert.equal(await hook(ctx('https://good.example/v1/search', '10000')), undefined);   // $0.01
  assert.equal(await hook(ctx('https://unknown.example/x', '10000')), undefined);        // $0.02
  const over = await hook(ctx('https://good.example/v1/search', '10000'));               // would be $0.03
  assert.equal(over.abort, true);
  assert.match(over.reason, /session budget: \$0\.0200 committed of \$0\.025/);
  assert.equal(hook.spent(), 0.02);

  const l = hook.ledger();
  assert.equal(l.length, 3);
  assert.deepEqual(l.map((d) => d.verdict), ['pass', 'unknown', 'over_budget']);
  assert.deepEqual(l.map((d) => d.aborted), [false, false, true]);
  assert.deepEqual(l[1].budget, { spent: 0.02, max: 0.025 });
  assert.ok(l[0].at);

  // Custom amount resolver (e.g. an 18-decimal asset).
  const h18 = deliveryGuard({ sessionBudget: { maxTotalUsd: 1 }, amountUsd: (r) => Number(r.amount) / 1e18, fetchImpl: fakeFetch(fixture).fn, logger: () => {} });
  assert.equal(await h18(ctx('https://unknown.example/x', '500000000000000000')), undefined);
  assert.equal(h18.spent(), 0.5);

  assert.throws(() => deliveryGuard({ sessionBudget: { maxTotalUsd: 0 } }), /maxTotalUsd/);
});

test('deliveryGuard: a blocked payment never counts against the budget', async () => {
  const hook = deliveryGuard({ sessionBudget: { maxTotalUsd: 0.01 }, fetchImpl: fakeFetch(fixture).fn, logger: () => {} });
  const ctx = (url) => ({ paymentRequired: { resource: { url }, accepts: [] }, selectedRequirements: { network: 'eip155:8453', amount: '10000' } });
  assert.equal((await hook(ctx('https://thief.example/v1/infer'))).abort, true);
  assert.equal(hook.spent(), 0);
  assert.equal(await hook(ctx('https://good.example/v1/search')), undefined);   // budget still available
  assert.equal(hook.spent(), 0.01);
});

test('deliveryGuard: abort / warn / strict modes and decision callback', async () => {
  const seen = [];
  const logs = [];
  const mk = (mode) => deliveryGuard({
    mode, fetchImpl: fakeFetch(fixture).fn, onDecision: (d) => seen.push(d.verdict), logger: (m) => logs.push(m),
  });
  const ctx = (url) => ({ paymentRequired: { resource: { url }, accepts: [] }, selectedRequirements: { network: 'eip155:8453', amount: '10000' } });

  const abort = mk('abort');
  assert.equal(await abort(ctx('https://good.example/v1/search')), undefined);
  assert.equal(await abort(ctx('https://unknown.example/x')), undefined);
  assert.equal(await abort(ctx('https://new.example/api/price')), undefined);         // warn only
  const blocked = await abort(ctx('https://thief.example/v1/infer'));
  assert.equal(blocked.abort, true);
  assert.match(blocked.reason, /took payment without delivering/);

  assert.equal(await mk('warn')(ctx('https://thief.example/v1/infer')), undefined);
  assert.equal((await mk('strict')(ctx('https://new.example/api/price'))).abort, true);

  assert.deepEqual(seen.slice(0, 4), ['pass', 'unknown', 'warn', 'block']);
  assert.ok(logs.some((m) => m.startsWith('BLOCK https://thief.example')));
  assert.throws(() => deliveryGuard({ mode: 'nope' }), /unknown mode/);
});

test('deliveryGuard: tolerates a context without a resource url and an observer that throws', async () => {
  const hook = deliveryGuard({ fetchImpl: fakeFetch(fixture).fn, onDecision: () => { throw new Error('x'); }, logger: () => {} });
  assert.equal(await hook({}), undefined);
  assert.equal(await hook({ paymentRequired: {} }), undefined);
});

test('checkUrl: carries entry + match for a hit, nulls for a miss', async () => {
  const source = createScoreSource({ fetchImpl: fakeFetch(fixture).fn });
  const hit = await checkUrl('https://recovered.example/v1/screen', { source });
  assert.equal(hit.match, 'exact');
  assert.equal(hit.entry.name, 'Recovered Screen');
  const miss = await checkUrl('https://nobody.example/', { source });
  assert.equal(miss.verdict, 'unknown');
  assert.equal(miss.entry, null);
});
