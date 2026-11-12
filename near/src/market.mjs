// NEAR Agent Market as a buyer: catalog, price, one paid hire over A2A + x402,
// and a plain rule for whether the hire delivered.

import { randomUUID } from 'node:crypto';
import { Account, JsonRpcProvider, actions, encodeSignedDelegate } from 'near-api-js';
import { NEAR_RPC, sha256 } from './pay.mjs';

export const MARKET_API = 'https://market.near.ai/v1';
export const NEAR_USDC = '17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1';
const TERMINAL = new Set(['TASK_STATE_COMPLETED', 'TASK_STATE_REJECTED', 'TASK_STATE_FAILED', 'TASK_STATE_CANCELED']);
// The verifier rejects a window at the full quoted timeout; stay inside it.
const TTL_FRACTION = 0.8;
const UA = { 'user-agent': 'agentpay-near-check/0.1' };

const json = async (res) => { try { return await res.json(); } catch { return null; } };
export const endpoint = (handle) => `https://${handle}.market.near.ai/a2a/v1`;
export const fromAtomic = (units) => (Number(units) / 1e6).toString();

/** Whole public catalog, each agent with its default per-call price. */
export async function catalog({ fetchImpl = globalThis.fetch, pageSize = 50, maxPages = 40, concurrency = 10 } = {}) {
  const agents = [];
  const seen = new Set();
  for (let page = 0; page < maxPages; page += 1) {
    const res = await fetchImpl(`${MARKET_API}/agents?limit=${pageSize}&offset=${page * pageSize}`, { headers: UA });
    const fresh = ((await json(res))?.agents || []).filter((a) => !seen.has(a.agent_id));
    if (!fresh.length) break;
    for (const a of fresh) { seen.add(a.agent_id); agents.push(a); }
    if (fresh.length < pageSize) break;
  }
  const queue = [...agents];
  await Promise.all(Array.from({ length: concurrency }, async () => {
    for (let a = queue.shift(); a; a = queue.shift()) {
      const plans = (await json(await fetchImpl(`${MARKET_API}/agents/${a.agent_id}/pricing-plans`, { headers: UA }).catch(() => null))) || [];
      const plan = plans.find((p) => p.is_default) || plans[0];
      a.price_usdc = plan ? Number(plan.price_amount) : null;
    }
  }));
  return agents;
}

const rpc = (method, params, id = 1) => ({ jsonrpc: '2.0', id, method, params });
const post = (url, body, headers = {}, fetchImpl = globalThis.fetch) =>
  fetchImpl(url, { method: 'POST', headers: { 'content-type': 'application/json', ...UA, ...headers }, body: JSON.stringify(body) });

/** One NEP-366 delegated ft_transfer for exactly the quoted row, as the X-PAYMENT header. */
export async function signPayment(row, { accountId, privateKey, rpcUrl = NEAR_RPC }) {
  const account = new Account(accountId, new JsonRpcProvider({ url: rpcUrl }), privateKey);
  const transfer = actions.functionCall('ft_transfer', { receiver_id: row.payTo, amount: row.amount }, 30000000000000n, 1n);
  const out = await account.createSignedMetaTransaction({ receiverId: row.asset, actions: [transfer], blockHeightTtl: Math.floor(row.maxTimeoutSeconds * TTL_FRACTION) });
  const signed = out?.signedDelegate ?? out;
  const payload = { signedDelegateAction: Buffer.from(encodeSignedDelegate(signed)).toString('base64') };
  return Buffer.from(JSON.stringify({ x402Version: 2, accepted: row, payload })).toString('base64');
}

/** Text of every artifact part; files count by their URL. */
export function artifactText(task) {
  return (task?.artifacts || []).flatMap((a) => a.parts || []).map((p) => p.text ?? p.url ?? (p.raw ? '[file]' : '')).join('\n').trim();
}

/** Delivered = completed with a non-empty artifact. */
export function hireDelivered(task) {
  return task?.status?.state === 'TASK_STATE_COMPLETED' && artifactText(task).length > 0;
}

/** Quote, then pay and wait. Refuses before signing when the price is over `maxUsdc`. */
export async function hire(handle, task, { accountId, privateKey, maxUsdc = '0.02', pollMs = 10000, waitMs = 120000, immediate = false, sign = signPayment, fetchImpl = globalThis.fetch, sleep = (ms) => new Promise((r) => setTimeout(r, ms)) } = {}) {
  const url = endpoint(handle);
  const message = { messageId: randomUUID(), role: 'ROLE_USER', parts: [{ text: task }] };
  const call = rpc('SendMessage', { message, configuration: { historyLength: 0 } });
  const out = { at: new Date().toISOString(), market: 'near-agent-market', handle, url, payer: accountId, max_usdc: String(maxUsdc), paid: false, delivered: false, refused: null };

  const t0 = Date.now();
  const quoteRes = await post(url, call, {}, fetchImpl);
  const quote = await json(quoteRes);
  out.unpaid = { status: quoteRes.status, ms: Date.now() - t0, rpc_error: quote?.error?.message ?? null };
  const row = (quote?.accepts || []).find((a) => String(a.network).startsWith('near:') && a.scheme === 'exact');
  if (quoteRes.status !== 402 || !row) { out.refused = out.unpaid.rpc_error || `no NEAR price quoted (HTTP ${quoteRes.status})`; return out; }
  Object.assign(out, { price_usdc: fromAtomic(row.amount), pay_to: row.payTo, network: row.network, window_s: row.maxTimeoutSeconds });
  if (Number(out.price_usdc) > Number(maxUsdc)) { out.refused = `price ${out.price_usdc} is over the cap ${maxUsdc}`; return out; }

  const t1 = Date.now();
  // `immediate` returns the task id at once; the caller polls GetTask later.
  const paidCall = immediate ? rpc('SendMessage', { message, configuration: { historyLength: 0, returnImmediately: true } }) : call;
  const paidRes = await post(url, paidCall, { 'X-PAYMENT': await sign(row, { accountId, privateKey }) }, fetchImpl);
  const body = await json(paidRes);
  let state = body?.result?.task;
  if (paidRes.status !== 200 || !state) {
    out.response = { status: paidRes.status, ms: Date.now() - t1, error: body?.error?.message || body?.error || null };
    return out;
  }
  out.paid = true; // an authorization is held; it settles only if the task completes
  while (!TERMINAL.has(state.status?.state) && Date.now() - t1 < waitMs) {
    await sleep(pollMs);
    const polled = (await json(await post(url, rpc('GetTask', { id: state.id, historyLength: 0 }, 2), {}, fetchImpl)))?.result;
    if (polled) state = polled;
  }
  const text = artifactText(state);
  out.delivered = hireDelivered(state);
  out.charged = state.status?.state === 'TASK_STATE_COMPLETED';
  out.response = { status: 200, ms: Date.now() - t1, task_id: state.id, state: state.status?.state ?? null, artifact_chars: text.length, artifact_sha256: text ? sha256(text) : null };
  return out;
}
