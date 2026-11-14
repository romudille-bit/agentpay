// NEAR Intents 1Click client: quote, deposit notice, status. Plain fetch, no key
// needed (a partner JWT only removes the 0.2% fee).

export const ONECLICK_URL = 'https://1click.chaindefuser.com/v0';

export const ASSETS = Object.freeze({
  wNEAR: { id: 'nep141:wrap.near', decimals: 24 },
  USDC_NEAR: { id: 'nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1', decimals: 6 },
  USDC_BASE: { id: 'nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near', decimals: 6 },
});

/** Decimal string → atomic units string, exact (no floats). */
export function toUnits(amount, decimals) {
  const s = String(amount).trim();
  if (!/^\d+(\.\d+)?$/.test(s)) throw new Error(`not a decimal amount: ${amount}`);
  const [whole, frac = ''] = s.split('.');
  if (frac.length > decimals) throw new Error(`${amount} has more than ${decimals} decimals`);
  return (BigInt(whole) * 10n ** BigInt(decimals) + BigInt(frac.padEnd(decimals, '0') || '0')).toString();
}

/** What the swap costs on top of the amount delivered, as a fraction. */
export function overhead(quote) {
  const inUsd = Number(quote?.amountInUsd);
  const outUsd = Number(quote?.amountOutUsd);
  if (!(inUsd > 0) || !(outUsd > 0)) return null;
  return inUsd / outUsd - 1;
}

export function createOneClick({ baseUrl = ONECLICK_URL, jwt = null, fetchImpl = globalThis.fetch } = {}) {
  async function call(path, init = {}) {
    const headers = { accept: 'application/json', ...(init.body ? { 'content-type': 'application/json' } : {}) };
    if (jwt) headers.authorization = `Bearer ${jwt}`;
    const res = await fetchImpl(`${baseUrl}${path}`, { ...init, headers });
    const text = await res.text();
    if (!res.ok) throw new Error(`1click ${path} ${res.status}: ${text.slice(0, 300)}`);
    return JSON.parse(text);
  }

  return {
    tokens: () => call('/tokens'),

    /** Quote for delivering exactly `usdcOut` USDC on Base to `recipient`.
     *  dry:true is a free preview; dry:false commits and returns a depositAddress. */
    async quoteUsdcOnBase({ usdcOut, recipient, refundTo, origin = 'USDC_NEAR', dry = true, slippageBps = 100, ttlMs = 10 * 60 * 1000, now = Date.now() }) {
      const from = ASSETS[origin];
      if (!from) throw new Error(`unknown origin asset: ${origin}`);
      const body = {
        dry,
        swapType: 'EXACT_OUTPUT',
        slippageTolerance: slippageBps,
        originAsset: from.id,
        depositType: 'ORIGIN_CHAIN',
        destinationAsset: ASSETS.USDC_BASE.id,
        amount: toUnits(usdcOut, ASSETS.USDC_BASE.decimals),
        refundTo,
        refundType: 'ORIGIN_CHAIN',
        recipient,
        recipientType: 'DESTINATION_CHAIN',
        deadline: new Date(now + ttlMs).toISOString(),
      };
      const out = await call('/quote', { method: 'POST', body: JSON.stringify(body) });
      return out.quote;
    },

    /** Quote for swapping exactly `amountIn` (atomic units of `origin`) into USDC on
     *  Base. An exact-input swap fills at market on arrival, so a small price move
     *  cannot strand it. */
    async quoteToBaseUsdc({ amountIn, recipient, refundTo, origin = 'USDC_NEAR', dry = true, slippageBps = 100, ttlMs = 30 * 60 * 1000, now = Date.now() }) {
      const from = ASSETS[origin];
      if (!from) throw new Error(`unknown origin asset: ${origin}`);
      const body = {
        dry,
        swapType: 'EXACT_INPUT',
        slippageTolerance: slippageBps,
        originAsset: from.id,
        depositType: 'ORIGIN_CHAIN',
        destinationAsset: ASSETS.USDC_BASE.id,
        amount: String(amountIn),
        refundTo,
        refundType: 'ORIGIN_CHAIN',
        recipient,
        recipientType: 'DESTINATION_CHAIN',
        deadline: new Date(now + ttlMs).toISOString(),
      };
      const out = await call('/quote', { method: 'POST', body: JSON.stringify(body) });
      return out.quote;
    },

    submitDeposit: (depositAddress, txHash) =>
      call('/deposit/submit', { method: 'POST', body: JSON.stringify({ depositAddress, txHash }) }),

    status: (depositAddress) => call(`/status?depositAddress=${encodeURIComponent(depositAddress)}`),
  };
}
