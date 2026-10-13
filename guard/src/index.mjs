// Delivery guard for the official x402 client: before a payment is signed,
// look the resource up in AgentPay's paid-probe scores and abort, warn or pass.
// Unknown resources always pass — absence of data is never a reason to block.

export const SCORES_URL = 'https://agentpay.tools/scores.json';
export const VERIFIED_ROUTE_URL = 'https://agentpay.tools/tools/verified_route/call';
export const DEFAULT_TTL_MS = 10 * 60 * 1000;
export const FLAG_NO_DELIVERY = 'took_payment_no_delivery';

// Thresholds mirror the prober's own scoring (probe.py): < 0.5 is "bad",
// >= 0.9 is "good"; between is mixed.
export const DEFAULT_THRESHOLDS = Object.freeze({ block: 0.5, pass: 0.9 });

/** Canonical form for URL matching: scheme+host lowercased, no query/fragment,
 *  no trailing slash. Returns null for anything that does not parse. */
export function normalizeUrl(input) {
  if (typeof input !== 'string' || !input) return null;
  let u;
  try { u = new URL(input); } catch { return null; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  const path = u.pathname.replace(/\/+$/, '');
  return `${u.protocol}//${u.host.toLowerCase()}${path}`;
}

/** Decide for one scores.json entry. Pure. */
export function decide(entry, thresholds = DEFAULT_THRESHOLDS) {
  if (!entry || !(entry.paid_probes > 0)) {
    return { verdict: 'unknown', reason: 'no paid probe in the scoring window' };
  }
  const rate = Number(entry.delivery_rate);
  const flags = Array.isArray(entry.flags) ? entry.flags : [];
  const flagged = flags.includes(FLAG_NO_DELIVERY);
  const lastFail = Date.parse(entry.last_fail_at || '') || 0;
  const lastOk = Date.parse(entry.last_ok_at || '') || 0;
  const why = entry.why ? ` — ${entry.why}` : '';

  // Block only on evidence of charging without delivering: flagged AND either
  // half or more of paid probes failed, or the latest probe is the failure.
  if (flagged && (rate <= thresholds.block || lastFail > lastOk)) {
    return {
      verdict: 'block',
      reason: `took payment without delivering (${pct(rate)} delivered over ${entry.paid_probes} paid probes)${why}`,
    };
  }
  if (flagged) {
    return { verdict: 'warn', reason: `took payment without delivering once, delivered since (${pct(rate)})${why}` };
  }
  if (rate < thresholds.block) {
    return { verdict: 'warn', reason: `low delivery rate (${pct(rate)} over ${entry.paid_probes} paid probes)${why}` };
  }
  if (entry.confidence === 'provisional' || rate < thresholds.pass || entry.settle_failures > 0) {
    return { verdict: 'warn', reason: `mixed evidence (${pct(rate)}, ${entry.confidence || 'unrated'})${why}` };
  }
  return { verdict: 'pass', reason: `${pct(rate)} delivered over ${entry.paid_probes} paid probes (${entry.confidence})${why}` };
}

function pct(rate) {
  return Number.isFinite(rate) ? `${Math.round(rate * 100)}%` : 'n/a';
}

/** Index of scores.json entries by normalized resource URL, with a prefix
 *  fallback for listings that score a base path and sell sub-paths. */
export class ScoreIndex {
  constructor(scores) {
    this.exact = new Map();
    this.byOrigin = new Map();
    this.byNeed = new Map();
    for (const e of scores?.services || []) {
      const key = normalizeUrl(e.resource_url);
      if (!key) continue;
      this.exact.set(key, e);
      const origin = key.slice(0, key.indexOf('/', 8));
      const list = this.byOrigin.get(origin) || [];
      list.push({ key, entry: e });
      this.byOrigin.set(origin, list);
      if (e.need) this.byNeed.set(e.need, [...(this.byNeed.get(e.need) || []), e]);
    }
  }

  get size() { return this.exact.size; }

  /** Scored providers for the same need that currently pass, excluding `entry`. */
  alternatives(entry, thresholds = DEFAULT_THRESHOLDS) {
    if (!entry?.need) return [];
    return (this.byNeed.get(entry.need) || [])
      .filter((e) => e !== entry && decide(e, thresholds).verdict === 'pass');
  }

  /** @returns {{ entry: object, match: 'exact'|'prefix' } | null} */
  lookup(url) {
    const key = normalizeUrl(url);
    if (!key) return null;
    const hit = this.exact.get(key);
    if (hit) return { entry: hit, match: 'exact' };
    const origin = key.slice(0, key.indexOf('/', 8));
    let best = null;
    for (const c of this.byOrigin.get(origin) || []) {
      if (key.startsWith(c.key + '/') && (!best || c.key.length > best.key.length)) best = c;
    }
    return best ? { entry: best.entry, match: 'prefix' } : null;
  }
}

/** Fetch scores.json with an in-memory TTL cache. Fails open: on any error
 *  the previous index is kept, or an empty one is returned. */
export function createScoreSource({ url = SCORES_URL, ttlMs = DEFAULT_TTL_MS, fetchImpl = globalThis.fetch } = {}) {
  let index = null;
  let fetchedAt = 0;
  let inflight = null;
  let lastError = null;

  async function refresh() {
    const res = await fetchImpl(url, { headers: { accept: 'application/json', 'user-agent': 'x402-guard' } });
    if (!res.ok) throw new Error(`scores fetch ${res.status}`);
    index = new ScoreIndex(await res.json());
    fetchedAt = Date.now();
    lastError = null;
    return index;
  }

  return {
    async get() {
      if (index && Date.now() - fetchedAt < ttlMs) return index;
      if (!inflight) {
        inflight = refresh().catch((e) => { lastError = e; return index || new ScoreIndex(null); })
          .finally(() => { inflight = null; });
      }
      return inflight;
    },
    get lastError() { return lastError; },
    get fetchedAt() { return fetchedAt; },
  };
}

/** One-off check of a resource URL. On block/warn, `alternative` points at
 *  verified_route for the same need when scored providers deliver for it. */
export async function checkUrl(url, { source = createScoreSource(), thresholds = DEFAULT_THRESHOLDS } = {}) {
  const idx = await source.get();
  const found = idx.lookup(url);
  if (!found) return { url, verdict: 'unknown', reason: 'not in scores.json', match: null, entry: null, alternative: null };
  const d = { url, ...decide(found.entry, thresholds), match: found.match, entry: found.entry, alternative: null };
  if (d.verdict === 'block' || d.verdict === 'warn') {
    const alts = idx.alternatives(found.entry, thresholds);
    if (alts.length) {
      d.alternative = { need: found.entry.need, scored_providers: alts.length, verified_route: VERIFIED_ROUTE_URL };
      d.reason += ` — ${alts.length} scored provider${alts.length > 1 ? 's' : ''} deliver for "${found.entry.need}"; ask verified_route`;
    }
  }
  return d;
}

/** Default USD resolver: amounts are atomic units of a 6-decimal stablecoin
 *  (USDC on every network x402 settles today). Override for other assets. */
export function amountUsdFromRequirements(req) {
  const n = Number(req?.amount);
  return Number.isFinite(n) ? n / 1e6 : 0;
}

/**
 * Build an `onBeforePaymentCreation` hook for `@x402/core`'s x402Client.
 *
 * mode: 'abort' aborts on block and warns on warn (default);
 *       'warn'  never aborts, only reports;
 *       'strict' aborts on block AND warn.
 * sessionBudget: { maxTotalUsd } — cumulative cap for this process; a payment
 *   that would take committed spend past it is aborted in every mode.
 * onDecision(decision) is called for every lookup; logger(message) on warn/block.
 * The returned hook exposes .ledger() (every decision) and .spent() (USD committed).
 */
export function deliveryGuard({
  mode = 'abort',
  scoresUrl = SCORES_URL,
  ttlMs = DEFAULT_TTL_MS,
  thresholds = DEFAULT_THRESHOLDS,
  sessionBudget = null,
  amountUsd = amountUsdFromRequirements,
  onDecision = null,
  logger = (m) => console.warn(`[x402-guard] ${m}`),
  fetchImpl = globalThis.fetch,
} = {}) {
  if (!['abort', 'warn', 'strict'].includes(mode)) throw new Error(`x402-guard: unknown mode ${mode}`);
  const maxTotal = sessionBudget?.maxTotalUsd;
  if (sessionBudget && !(Number(maxTotal) > 0)) throw new Error('x402-guard: sessionBudget.maxTotalUsd must be > 0');
  const source = createScoreSource({ url: scoresUrl, ttlMs, fetchImpl });
  const ledger = [];
  let spent = 0;

  async function beforePaymentCreation(ctx) {
    const url = ctx?.paymentRequired?.resource?.url;
    const req = ctx?.selectedRequirements;
    const decision = await checkUrl(url, { source, thresholds });
    decision.network = req?.network ?? null;
    decision.amount = req?.amount ?? null;
    decision.amountUsd = amountUsd(req);
    decision.at = new Date().toISOString();

    let abortReason = null;
    if (decision.verdict === 'block' && mode !== 'warn') abortReason = decision.reason;
    else if (decision.verdict === 'warn' && mode === 'strict') abortReason = decision.reason;

    // Budget is checked after the delivery verdict so a blocked seller never
    // counts against the cap, and it aborts regardless of mode.
    if (!abortReason && maxTotal && spent + decision.amountUsd > maxTotal) {
      abortReason = `session budget: $${spent.toFixed(4)} committed of $${maxTotal}, this payment $${decision.amountUsd.toFixed(4)}`;
      decision.verdict = 'over_budget';
      decision.reason = abortReason;
    }
    decision.aborted = Boolean(abortReason);
    if (!abortReason) spent += decision.amountUsd;
    decision.budget = maxTotal ? { spent, max: maxTotal } : null;

    ledger.push(decision);
    if (onDecision) { try { onDecision(decision); } catch { /* observer errors never block a payment */ } }
    if (decision.verdict !== 'pass' && decision.verdict !== 'unknown') {
      logger(`${decision.verdict.toUpperCase()} ${url}: ${decision.reason}${abortReason ? ' — payment aborted' : ''}`);
    }
    return abortReason ? { abort: true, reason: `x402-guard: ${abortReason}` } : undefined;
  }

  beforePaymentCreation.ledger = () => ledger.slice();
  beforePaymentCreation.spent = () => spent;
  return beforePaymentCreation;
}
