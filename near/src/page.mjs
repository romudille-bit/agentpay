// Build docs/index.html from the hire log: what we paid for on NEAR and what came back.
// The data is inlined, so the page opens from disk as well as from GitHub Pages.

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('..', import.meta.url));
const MARKET_API = 'https://market.near.ai/v1';
const UA = { 'user-agent': 'agentpay-near-check/0.1' };
const read = (p, d) => (existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')) : d);

/** One label per hire. Only hires that ran their course count in the rate. */
export function outcome(row) {
  const state = row.response?.state;
  if (row.refused) return 'unavailable';
  if (row.delivered) return 'delivered';
  if (state === 'TASK_STATE_REJECTED') return 'rejected';
  // We cancel a task that has not started. Inside the agent's listed deadline that is our call, not its failure.
  if (state === 'TASK_STATE_CANCELED') return row.sla_s && (row.response.ms || 0) / 1000 < row.sla_s ? 'cut_short' : 'no_start';
  if (state === 'TASK_STATE_COMPLETED') return 'empty';
  if (Date.now() > Date.parse(row.at) + (row.window_s || 0) * 1000) return 'no_answer';
  return 'working';
}

export const LABEL = {
  delivered: 'delivered',
  rejected: 'rejected the task',
  no_start: 'had not started when we cancelled',
  cut_short: 'cancelled by us before its deadline',
  no_answer: 'no answer in the window',
  empty: 'completed, nothing attached',
  unavailable: 'not reachable, not hired',
  working: 'still working',
};

/** Hires from the sweep log plus any one-off hire receipts, newest row per agent. */
export function hires(root = here) {
  const rows = read(`${root}docs/hires.json`, []).filter((r) => !r.void);
  const dir = `${root}receipts`;
  if (existsSync(dir)) {
    // One-off hires live in their own receipt; an agent hired twice keeps its last try.
    const extra = new Map();
    for (const f of readdirSync(dir).filter((n) => n.endsWith('.json'))) {
      const r = read(`${dir}/${f}`, null);
      if (r?.market !== 'near-agent-market' || rows.some((x) => x.handle === r.handle)) continue;
      const held = extra.get(r.handle);
      if (!held || held.at < r.at) extra.set(r.handle, r);
    }
    rows.push(...extra.values());
  }
  return rows.sort((a, b) => a.at.localeCompare(b.at));
}

/** Direct x402 sellers paid outside the marketplace, from the buy receipts. */
export function buys(root = here) {
  const dir = `${root}receipts`;
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .map((f) => read(`${dir}/${f}`, null))
    .filter((r) => r?.url && !r.market)
    .sort((a, b) => a.at.localeCompare(b.at));
}

/** What the catalog claims about each agent, so the page can set it beside the result. */
export async function claims(handles, { fetchImpl = globalThis.fetch } = {}) {
  const out = {};
  await Promise.all(handles.map(async (h) => {
    try {
      const res = await fetchImpl(`${MARKET_API}/agents/${h}`, { headers: UA });
      const a = await res.json();
      if (a?.handle) out[h] = { jobs: a.delivered_jobs ?? 0, success: a.success_rate ?? null, available: a.available_now ?? null, name: a.name ?? h };
    } catch { /* the page states what it could not read */ }
  }));
  return out;
}

/** Seconds a buyer spent on one hire: the work it returned, or the wait it cost. */
export function spent(row) {
  const o = outcome(row);
  if (o === 'unavailable' || o === 'working' || o === 'cut_short') return 0;
  if (o === 'delivered') return (row.response.ms || 0) / 1000;
  // An agent that stops answering burns its whole payment window before the
  // buyer can be sure; a cancelled or refused one costs only what it held.
  return o === 'no_answer' ? (row.window_s || 0) : (row.response?.ms || 0) / 1000;
}

export function summarise(rows) {
  // Hired = a price was quoted and a payment signed. The rate covers hires that ran
  // their course: not ones still open, and not ones we cut short ourselves.
  const hired = rows.filter((r) => outcome(r) !== 'unavailable');
  const cutShort = hired.filter((r) => outcome(r) === 'cut_short');
  const settled = hired.filter((r) => !['working', 'cut_short'].includes(outcome(r)));
  const delivered = settled.filter((r) => outcome(r) === 'delivered');
  const failed = settled.filter((r) => outcome(r) !== 'delivered');
  const times = delivered.map((r) => r.response.ms).filter(Number.isFinite).sort((a, b) => a - b);
  return {
    hired: hired.length,
    settled: settled.length,
    cut_short: cutShort.length,
    unreachable: rows.length - hired.length,
    delivered: delivered.length,
    failed: failed.length,
    rate: settled.length ? Math.round((delivered.length / settled.length) * 100) : 0,
    fail_rate: settled.length ? Math.round((failed.length / settled.length) * 100) : 0,
    charged: hired.filter((r) => r.charged).reduce((s, r) => s + Number(r.price_usdc || 0), 0).toFixed(2),
    quoted: hired.reduce((s, r) => s + Number(r.price_usdc || 0), 0).toFixed(2),
    median_ms: times.length ? times[Math.floor(times.length / 2)] : null,
    waited_h: +(failed.reduce((s, r) => s + spent(r), 0) / 3600).toFixed(1),
    worked_min: +(delivered.reduce((s, r) => s + spent(r), 0) / 60).toFixed(1),
    at: new Date().toISOString(),
  };
}

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function render({ rows, direct, claim, sum, fund }) {
  const tile = (v, k) => `<div class="tile"><b>${esc(v)}</b><span>${esc(k)}</span></div>`;
  const secs = (ms) => (ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
  const cls = { delivered: 'ok', rejected: 'warn', no_start: 'bad', cut_short: 'mut', no_answer: 'bad', empty: 'bad', unavailable: 'mut', working: 'mut' };

  const hireRows = rows.map((r) => {
    const o = outcome(r);
    const c = claim[r.handle];
    const says = c
      ? `${c.jobs} job${c.jobs === 1 ? '' : 's'}${c.success == null ? '' : `, ${c.success}%`}`
      : '<span class="mut">—</span>';
    const got = o === 'delivered' ? `${r.response.artifact_chars.toLocaleString()} chars` : '<span class="mut">nothing</span>';
    return `<tr>
      <td><a href="https://market.near.ai/agents/${esc(r.handle)}">${esc(r.handle)}</a></td>
      <td>$${esc(r.price_usdc ?? '—')}</td>
      <td class="${cls[o]}">${esc(LABEL[o])}</td>
      <td>${got}</td>
      <td>${esc(secs(o === 'delivered' ? r.response.ms : null))}</td>
      <td>${r.charged ? (r.settle_tx ? `<a href="https://nearblocks.io/txns/${esc(r.settle_tx)}" title="settlement transaction">$${esc(r.price_usdc)}</a>` : `$${esc(r.price_usdc)}`) : '<span class="mut">nothing</span>'}</td>
      <td class="mut">${says}</td>
    </tr>`;
  }).join('');

  const directRows = direct.map((r) => `<tr>
      <td class="ep" title="${esc(r.url)}">${esc(r.url.replace(/^https?:\/\//, '').split('?')[0])}</td>
      <td>${esc(r.rail === 'near' ? 'USDC on NEAR' : 'USDC on Base, funded through NEAR Intents')}</td>
      <td>$${esc(r.price_usdc ?? '—')}</td>
      <td class="${r.delivered ? 'ok' : 'bad'}">${r.delivered ? 'delivered' : esc(String(r.refused || (r.response && !r.response.settle ? `not paid — seller answered HTTP ${r.response.status}` : 'not delivered')).split(';')[0])}</td>
      <td>${esc(secs(r.response?.ms))}${r.response?.ms && r.rail !== 'near' ? ' <span class="mut" title="the funding swap had to land before this call could be made">+ funding</span>' : ''}</td>
      <td class="ep">${r.response?.settle?.transaction ? `<code>${esc(String(r.response.settle.transaction).slice(0, 10))}…</code>` : '<span class="mut">—</span>'}</td>
    </tr>`).join('');

  const fundRows = (fund || []).map((f) => `<tr>
      <td class="ep" title="${esc(f.address)}"><code>${esc(f.address.slice(0, 10))}…</code></td>
      <td>${esc(f.sent == null ? '—' : `${Number(f.sent).toFixed(4)} ${f.sent_unit || ''}`.trim())}</td>
      <td class="${f.status === 'SUCCESS' ? 'ok' : 'warn'}">${esc(f.status === 'SUCCESS' ? 'delivered' : `refunded — ${String(f.reason || '').toLowerCase().replace(/_/g, ' ')}`)}</td>
      <td>${esc(f.received ? `${Number(f.received).toFixed(4)} USDC on Base` : 'nothing')}</td>
      <td>${esc(f.seconds == null ? '—' : f.seconds < 60 ? `${f.seconds} s` : f.seconds < 3600 ? `${Math.round(f.seconds / 60)} min` : `${(f.seconds / 3600).toFixed(1)} h`)}</td>
    </tr>`).join('');

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paid on NEAR: what came back</title>
<meta name="description" content="We hired agents on NEAR Agent Market with real USDC and wrote down what came back: delivered, never started, or no answer.">
<style>
  :root{--bg:#f7f7f5;--fg:#17191c;--mut:#6b7079;--line:#dfe1e4;--card:#fff;--ok:#1f7a4d;--bad:#b3261e;--warn:#9a6700;--acc:#1f4fd8;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
  @media (prefers-color-scheme:dark){:root{--bg:#111316;--fg:#e8e9eb;--mut:#9aa0a8;--line:#2a2e34;--card:#181b1f;--ok:#4cc38a;--bad:#ff6b63;--warn:#e2b340;--acc:#7aa2ff}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  main{max-width:1100px;margin:0 auto;padding:40px 16px 64px}
  h1{font-size:28px;margin:0 0 4px;letter-spacing:-.01em}
  .sub{color:var(--mut);margin:0 0 24px;max-width:760px}
  a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:0 0 20px}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .tile b{display:block;font-size:24px;font-weight:600;letter-spacing:-.01em}
  .tile span{color:var(--mut);font-size:13px}
  .lede{max-width:820px;margin:0 0 14px}
  .lede:last-of-type{margin-bottom:26px}
  .wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
  table{border-collapse:collapse;width:100%;min-width:760px;font-size:13.5px}
  th,td{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
  th{font-weight:600;color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em;background:var(--bg)}
  tr:last-child td{border-bottom:0}
  td.ep{max-width:320px;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono);font-size:12px}
  .ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}
  h2{font-size:17px;margin:34px 0 8px}
  .notes{color:var(--mut);font-size:14px;max-width:820px}
  .notes dt{font-weight:600;color:var(--fg);margin-top:10px}
  .notes dd{margin:2px 0 0}
  code{font-family:var(--mono);font-size:12px}
</style>
</head>
<body>
<main>
  <h1>${sum.failed} of the ${sum.settled} agents we hired returned nothing</h1>
  <p class="sub">Every listed agent on <a href="https://market.near.ai">NEAR Agent Market</a> is payable per call in USDC on NEAR. We hired the cheapest available ones on mainnet with real money, one at a time, and wrote down what came back.${sum.cut_short ? ` ${sum.cut_short} more hires we cancelled ourselves before the agent's own deadline; they are listed and left out of the count.` : ''} By <a href="https://agentpay.tools">AgentPay</a>.</p>
  <div class="tiles">
    ${[tile(sum.settled, 'hires that ran their course'), tile(`${sum.fail_rate}%`, 'returned nothing'), tile(sum.delivered, 'delivered'), tile(`${sum.waited_h} h`, 'spent waiting for results that never came'), tile(`${sum.worked_min} min`, 'of work received'), tile(`$${sum.charged}`, `charged of $${sum.quoted} quoted`)].join('\n    ')}
  </div>
  <p class="lede">Nothing here was charged for a failure — the marketplace holds each payment until the agent delivers, and that part is exact. The cost is time: an agent that goes quiet burns its whole payment window before a buyer can be sure. We spent ${esc(sum.waited_h)} hours waiting on hires that returned nothing, against ${esc(sum.worked_min)} minutes of delivered work.</p>
  <p class="lede">A buyer cannot tell the two apart beforehand. The catalog's own track record does not separate them: the agents below with the longest delivered-job histories are among those that never answered. Paying and watching is the only signal that holds, which is why <a href="https://agentpay.tools">AgentPay</a> sells one — <code>verified_route</code> ranks x402 endpoints by whether a paid probe actually got data back. The marketplace already holds what it needs to show the same thing: delivery measured over paid hires, and the date an agent last delivered.</p>

  <h2>Hires on NEAR Agent Market</h2>
  <div class="wrap"><table><thead><tr>
    <th>Agent</th><th>Price</th><th>What happened</th><th>Came back</th><th>Took</th><th>Charged</th><th>Catalog says</th>
  </tr></thead><tbody>${hireRows || '<tr><td colspan="7" class="mut">No hires recorded yet.</td></tr>'}</tbody></table></div>

  <h2>x402 sellers paid directly</h2>
  <p class="notes">The same buyer, paying ordinary x402 endpoints instead of the marketplace: one that takes USDC on NEAR, one on Base paid from wallets funded through NEAR Intents. Rows that were not paid cost nothing: one waited on a swap that was still in flight, and one we sent without a parameter the seller requires.</p>
  <div class="wrap"><table><thead><tr>
    <th>Endpoint</th><th>Paid with</th><th>Price</th><th>Result</th><th>Took</th><th>Settlement</th>
  </tr></thead><tbody>${directRows || '<tr><td colspan="6" class="mut">No direct purchases recorded yet.</td></tr>'}</tbody></table></div>

  <h2>Funding a Base payment from a NEAR balance</h2>
  <p class="notes">Each quote for this route estimated 22 to 24 seconds. On the morning of 12 Nov 2026 three attempts took far longer: the first two sent NEAR and were refunded as expired quotes; the third sent USDC and arrived after more than two hours. The second and third resolved within the same minute, which looks like a stall that morning rather than the usual wait. On 14 Nov the same route, sent as USDC by the buyer in this repository, arrived in 26 seconds. Each wait runs from our deposit transaction to the refund or delivery transaction. A refund returns the deposit in full. We reported the stuck swaps to NEAR support while they were pending: the first reply said they were processing and to wait, the second confirmed a refund for an expired quote. A swap costs about a quarter of a cent plus 1%, so the buyer funds several purchases at once rather than one.</p>
  <div class="wrap"><table><thead><tr>
    <th>Deposit address</th><th>Sent</th><th>Result</th><th>Received</th><th>Took</th>
  </tr></thead><tbody>${fundRows || '<tr><td colspan="5" class="mut">No funding attempts recorded.</td></tr>'}</tbody></table></div>

  <h2>What the columns mean</h2>
  <dl class="notes">
    <dt>What happened</dt>
    <dd><b>delivered</b> — the task completed and something came back. Any non-empty result counts, we do not grade it, so the share that returned nothing is a floor. <b>no answer in the window</b> — the agent accepted the job and never returned a result before the payment expired. <b>had not started when we cancelled</b> — the task was still waiting to start after the agent's listed deadline, so we cancelled; a cancelled task moves no money. <b>rejected the task</b> — the agent declined it. <b>cancelled by us before its deadline</b> — our sweep gave up on a task that had not started while the agent still had time; not counted either way. <b>not reachable, not hired</b> — the marketplace could not reach the agent, so no price was quoted and nothing was signed; not counted.</dd>
    <dt>Charged</dt>
    <dd>The marketplace holds the payment and settles it only on delivery, so everything other than a delivered hire cost nothing. Each charged amount links to its settlement transaction. That part works: the gap is between what the catalog advertises and what a buyer gets. Checked on chain on 14 Nov: the payer's USDC balance on NEAR fell by exactly $0.916561 — $0.50 for the delivered hires, $0.01 for the direct purchase, $0.406561 sent to Base through NEAR Intents in two swaps.</dd>
    <dt>Catalog says</dt>
    <dd>The delivered-job count and success rate the marketplace shows for that agent, read at build time.</dd>
    <dt>Came back</dt>
    <dd>Size only. Response bodies are never published here; the hire log keeps a SHA-256 of each one.</dd>
  </dl>
  <p class="notes">One brief per agent, written to suit what it advertises. An agent that fails here may do better on a different task. ${sum.settled} hires is a small sample, taken over two days. Source and method: <a href="https://github.com/romudille-bit/agentpay/tree/main/near">github.com/romudille-bit/agentpay/near</a> (the judged version is the <a href="https://github.com/romudille-bit/agentpay/tree/hackathon-submission/near"><code>hackathon-submission</code></a> tag).</p>
</main>
</body>
</html>
`;
}

if (fileURLToPath(import.meta.url) === process.argv[1]) {
  const root = process.argv.includes('--root') ? process.argv[process.argv.indexOf('--root') + 1] : here;
  const rows = hires(root);
  const claim = process.argv.includes('--no-fetch') ? {} : await claims([...new Set(rows.map((r) => r.handle))]);
  const sum = summarise(rows);
  mkdirSync(`${root}docs`, { recursive: true });
  writeFileSync(`${root}docs/index.html`, render({ rows, direct: buys(root), claim, sum, fund: read(`${root}docs/funding.json`, []) }));
  console.log(JSON.stringify(sum));
}
