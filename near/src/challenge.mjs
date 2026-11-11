// Read a seller's unpaid 402 and decide which rail a NEAR-funded agent uses.

export const NEAR_MAINNET = 'near:mainnet';
export const BASE_MAINNET = 'eip155:8453';
const V1_NETWORKS = { base: BASE_MAINNET, near: NEAR_MAINNET };

/** PAYMENT-REQUIRED header (base64 JSON, x402 v2) → object, or null. */
export function decodePaymentRequired(value) {
  if (!value) return null;
  try { return JSON.parse(Buffer.from(value, 'base64').toString('utf8')); } catch { return null; }
}

/** Normalised offers from a v2 header or a v1 JSON body. */
export function offers(challenge) {
  return (challenge?.accepts || []).map((a) => ({
    scheme: a.scheme,
    network: V1_NETWORKS[a.network] || a.network,
    amount: String(a.amount ?? a.maxAmountRequired ?? ''),
    asset: a.asset,
    payTo: a.payTo,
    raw: a,
  })).filter((a) => a.scheme === 'exact' && /^\d+$/.test(a.amount));
}

/** NEAR first (direct, no swap), then Base through Intents. */
export function pickRail(challenge) {
  const list = offers(challenge);
  const near = list.find((a) => a.network === NEAR_MAINNET);
  if (near) return { rail: 'near', offer: near };
  const base = list.find((a) => a.network === BASE_MAINNET);
  if (base) return { rail: 'base-via-intents', offer: base };
  return { rail: null, offer: null };
}

/** One unpaid request. Never throws on HTTP status. */
export async function readChallenge(url, { method = 'GET', body = undefined, fetchImpl = globalThis.fetch } = {}) {
  const started = Date.now();
  const res = await fetchImpl(url, {
    method,
    headers: { accept: 'application/json', ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const latencyMs = Date.now() - started;
  let challenge = decodePaymentRequired(res.headers.get('payment-required'));
  if (!challenge && res.status === 402) {
    try { const j = await res.json(); if (Array.isArray(j?.accepts)) challenge = j; } catch { /* not JSON */ }
  }
  return { status: res.status, latencyMs, challenge };
}
