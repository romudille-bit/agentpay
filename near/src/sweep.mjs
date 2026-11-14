// Hire every agent in targets.json once, one at a time, under a per-hire cap, and
// keep what came back in docs/hires.json. The market holds each payment until the
// agent delivers, and held payments from one key share a sequence number, so a new
// hire starts only when the previous one has ended. Re-running resumes.

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { artifactText, committedUsdc, endpoint, hire, hireDelivered } from './market.mjs';
import { sha256 } from './pay.mjs';

const here = fileURLToPath(new URL('..', import.meta.url));
const OUT = `${here}docs/hires.json`;
const load = (p, d) => (existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')) : d);
const TERMINAL = new Set(['TASK_STATE_COMPLETED', 'TASK_STATE_REJECTED', 'TASK_STATE_FAILED', 'TASK_STATE_CANCELED']);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const UA = { 'user-agent': 'agentpay-near-check/0.1' };

const expired = (row) => Date.now() > Date.parse(row.at) + (row.window_s || 0) * 1000;
const ended = (row) => Boolean(row.refused) || !row.response?.task_id || TERMINAL.has(row.response.state) || expired(row);

async function poll(row) {
  const res = await fetch(endpoint(row.handle), { method: 'POST', headers: { 'content-type': 'application/json', ...UA }, body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'GetTask', params: { id: row.response.task_id, historyLength: 0 } }) });
  const task = (await res.json().catch(() => null))?.result;
  if (!task) return;
  const text = artifactText(task);
  Object.assign(row.response, { state: task.status?.state ?? row.response.state, artifact_chars: text.length, artifact_sha256: text ? sha256(text) : null });
  row.delivered = hireDelivered(task);
  row.charged = task.status?.state === 'TASK_STATE_COMPLETED';
  if (TERMINAL.has(row.response.state)) row.response.ms = Date.parse(task.status?.timestamp || new Date().toISOString()) - Date.parse(row.at);
}

const { NEAR_ACCOUNT_ID: accountId, NEAR_PRIVATE_KEY: privateKey } = process.env;
const args = process.argv.slice(2);
const opt = (name, d) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : d; };
const budgetMs = Number(opt('budget-s', '150')) * 1000;
const maxUsdc = opt('max', '0.10');
// Ceiling for the whole log: charged hires plus any still on hold. Each hire's cap shrinks to what is left.
const totalUsdc = opt('total', '2.00');
const pollOnly = args.includes('--poll');
const targets = load(`${here}targets.json`, []);
const rows = load(OUT, []);
const save = () => writeFileSync(OUT, JSON.stringify(rows, null, 1));
const started = Date.now();
const left = () => budgetMs - (Date.now() - started);

/** A task still waiting to start after the agent's listed SLA is cancelled; a cancelled task moves no money. */
async function cancelIfNeverStarted(row) {
  // The listed SLA, and never less than two minutes. The first sweep capped this
  // at two minutes; hires it cancelled early are kept out of the rate on the page.
  const slaMs = Math.max(120, row.sla_s || 120) * 1000;
  if (row.response?.state !== 'TASK_STATE_SUBMITTED' || Date.now() - Date.parse(row.at) < slaMs) return;
  await fetch(endpoint(row.handle), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 3, method: 'CancelTask', params: { id: row.response.task_id } }) }).catch(() => {});
  await poll(row).catch(() => {});
  if (row.response.state === 'TASK_STATE_CANCELED') row.response.note = 'not started within its listed SLA; cancelled by us, no charge';
}

/** Poll one row until it ends or the run's time budget is nearly spent. */
async function settle(row) {
  while (!ended(row) && left() > 8000) { await sleep(5000); await poll(row).catch(() => {}); await cancelIfNeverStarted(row); save(); }
  return ended(row);
}

let waitingOn = null;
for (const row of rows) {
  if (row.void || ended(row)) continue;
  console.error(`${row.handle}: still working from an earlier run, waiting...`);
  await poll(row).catch(() => {}); await cancelIfNeverStarted(row); save();
  if (!(await settle(row))) { waitingOn = row.handle; break; }
}
if (!pollOnly && !waitingOn) {
  if (!accountId || !privateKey) { console.error('set NEAR_ACCOUNT_ID and NEAR_PRIVATE_KEY'); process.exit(78); }
  for (const t of targets) {
    if (left() < 30000) break;
    if (rows.some((r) => r.handle === t.handle && !r.void)) continue;
    const leftUsdc = Number(totalUsdc) - Number(committedUsdc(rows));
    if (leftUsdc <= 0) { console.error(`total budget $${totalUsdc} reached; stopping`); break; }
    const cap = Math.min(Number(maxUsdc), leftUsdc).toFixed(6);
    const r = await hire(t.handle, t.task, { accountId, privateKey, maxUsdc: cap, waitMs: 0, immediate: true })
      .catch((e) => ({ at: new Date().toISOString(), handle: t.handle, paid: false, delivered: false, refused: `error: ${e.message}` }));
    // No task came back (facilitator or market error): that is not a result. Stop
    // the run instead of recording it or hammering the next agent.
    if (!r.refused && !r.response?.task_id) {
      console.error(`${t.handle}: no task returned (HTTP ${r.response?.status ?? '?'} ${r.response?.error ?? ''}); stopping, run again in a few minutes`);
      waitingOn = `${t.handle} (market error)`;
      break;
    }
    if (/over the cap/.test(r.refused || '') && Number(cap) < Number(maxUsdc)) { console.error(`${t.handle}: ${r.refused} (what is left of the $${totalUsdc} total); stopping`); break; }
    r.title = t.task.split('\n')[0];
    r.sla_s = t.sla_s ?? null;
    rows.push(r); save();
    if (!ended(r)) console.error(`${t.handle}: hired at $${r.price_usdc}, waiting for the result...`);
    const done = await settle(r);
    console.error(`${t.handle}: ${r.refused ? `refused (${r.refused})` : `${r.response?.state ?? `HTTP ${r.response?.status}`} $${r.price_usdc ?? '?'} ${r.response?.artifact_chars ?? 0} chars`}`);
    if (!done) { waitingOn = r.handle; break; }
  }
}
for (const r of rows) if (!r.void && r.response?.task_id && !TERMINAL.has(r.response.state) && expired(r)) r.response.note = 'no result within the payment window; nothing charged';
save();
const live = rows.filter((r) => !r.void);
console.log(JSON.stringify({ targets: targets.length, hired: live.length, delivered: live.filter((r) => r.delivered).length, refused: live.filter((r) => r.refused).length, waiting_on: waitingOn }));
