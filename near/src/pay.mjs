// The two paying legs for a NEAR-funded agent: x402 direct on NEAR, and
// x402 on Base from a throwaway wallet funded through NEAR Intents.

import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { createNearPaymentFetch } from '@fastnear/x402';
import { createLocalNearSigner } from '@fastnear/x402/node';
import { x402Client } from '@x402/core/client';
import { decodePaymentResponseHeader } from '@x402/core/http';
import { registerExactEvmScheme } from '@x402/evm/exact/client';
import { wrapFetchWithPayment } from '@x402/fetch';
import { Account, JsonRpcProvider } from 'near-api-js';
import { FungibleToken } from 'near-api-js/tokens';
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';
import { BASE_MAINNET, BASE_USDC, NEAR_MAINNET, NEAR_USDC, decodePaymentRequired, isUsdc } from './challenge.mjs';
import { createOneClick, toUnits } from './oneclick.mjs';

export const NEAR_RPC = 'https://rpc.mainnet.fastnear.com';
export const BASE_RPC = 'https://mainnet.base.org';
export { BASE_USDC };
const TERMINAL = new Set(['SUCCESS', 'REFUNDED', 'FAILED']);

export const sha256 = (text) => createHash('sha256').update(text).digest('hex');

/** 2xx with a non-empty body that is not just an error object. */
export function deliveredFrom(status, text) {
  if (status < 200 || status >= 300) return false;
  if (!text || !text.trim()) return false;
  try {
    const j = JSON.parse(text);
    return !(j && typeof j === 'object' && 'error' in j && Object.keys(j).length <= 2);
  } catch { return true; }
}

const init = (method, body) => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body), headers: { 'content-type': 'application/json' } }),
});

/** Response → what the buyer got. The body itself is never kept. */
export async function outcome(res, startedAt) {
  const text = await res.text();
  const header = res.headers.get('payment-response');
  let settle = null;
  if (header) { try { settle = decodePaymentResponseHeader(header); } catch { settle = { raw: header }; } }
  const delivered = deliveredFrom(res.status, text);
  return {
    status: res.status,
    ms: Date.now() - startedAt,
    delivered,
    body_bytes: Buffer.byteLength(text),
    body_sha256: text ? sha256(text) : null,
    error_body: delivered ? null : text.slice(0, 240),
    settle,
  };
}

/** Thrown before signing when a 402 asks for more than the cap or for another token. */
export class OverCap extends Error {}

/**
 * fetch that reads every 402 before the payer does. The paying request gets its
 * own quote, so the cap is checked here, on the quote that would be signed.
 */
export function cappedFetch(fetchImpl, maxUsdc) {
  const cap = BigInt(toUnits(maxUsdc, 6));
  return async (...args) => {
    const res = await fetchImpl(...args);
    if (res.status !== 402) return res;
    let challenge = decodePaymentRequired(res.headers.get('payment-required'));
    if (!challenge) { try { challenge = await res.clone().json(); } catch { challenge = null; } }
    if (!Array.isArray(challenge?.accepts)) throw new OverCap('seller sent a 402 with no readable price; nothing signed');
    for (const a of challenge.accepts) {
      if (!String(a.network).startsWith('near')) continue;
      const amount = String(a.amount ?? a.maxAmountRequired ?? '');
      if (!isUsdc(NEAR_MAINNET, a.asset)) throw new OverCap(`seller asked to be paid in ${a.asset}, not USDC`);
      if (!/^\d+$/.test(amount) || BigInt(amount) > cap) throw new OverCap(`seller asked for ${amount} units at payment time, over the cap ${maxUsdc}`);
    }
    return res;
  };
}

export async function payNear(url, { accountId, privateKey, maxUsdc, method = 'GET', body, rpcUrl = NEAR_RPC, fetchImpl = globalThis.fetch } = {}) {
  if (maxUsdc == null) throw new Error('payNear needs maxUsdc');
  const signer = createLocalNearSigner({ accountId, secretKey: privateKey, rpcUrls: { [NEAR_MAINNET]: rpcUrl } });
  const paidFetch = createNearPaymentFetch({ signer, network: NEAR_MAINNET, fetch: cappedFetch(fetchImpl, maxUsdc) });
  const started = Date.now();
  return outcome(await paidFetch(url, init(method, body)), started);
}

const erc20Balance = async (address, { rpcUrl = BASE_RPC, fetchImpl = globalThis.fetch } = {}) => {
  const res = await fetchImpl(rpcUrl, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'eth_call', params: [{ to: BASE_USDC, data: `0x70a08231${address.slice(2).toLowerCase().padStart(64, '0')}` }, 'latest'] }),
  });
  return BigInt((await res.json()).result || '0x0');
};

/** Session wallets on disk (gitignored), each with its live USDC balance on Base. */
export async function sessionWallets(walletDir, opts) {
  if (!walletDir || !existsSync(walletDir)) return [];
  const out = [];
  for (const f of readdirSync(walletDir).filter((n) => n.endsWith('.json'))) {
    const w = JSON.parse(readFileSync(`${walletDir}/${f}`, 'utf8'));
    out.push({ ...w, usdc_atomic: await erc20Balance(w.address, opts).catch(() => 0n) });
  }
  return out;
}

/** USDC on NEAR to `to`. A deposit address is new, so its token storage is paid first. */
async function sendUsdc({ accountId, privateKey, rpcUrl, to, amount }) {
  const provider = new JsonRpcProvider({ url: rpcUrl });
  const account = new Account(accountId, provider, privateKey);
  const token = new FungibleToken(NEAR_USDC, { decimals: 6, symbol: 'USDC' });
  if (!(await token.isAccountRegistered({ accountId: to, provider }))) await token.registerAccount({ accountIdToRegister: to, fundingAccount: account });
  const sent = await account.transfer({ token, amount, receiverId: to });
  return sent?.transaction?.hash ?? sent?.transaction_outcome?.id ?? null;
}

/** Top a fresh Base wallet up with about `usdc` by sending USDC on NEAR to a 1Click
 *  deposit address. The agent spends one asset on either chain; the deposit itself needs a
 *  little NEAR for gas and for token storage on the new deposit address. The
 *  wallet file is written before any money moves, so a later run can pick a slow
 *  swap up. */
export async function fundBaseWallet({ accountId, privateKey, usdc, rpcUrl = NEAR_RPC, oneClick = createOneClick(), walletDir = null, pollMs = 5000, timeoutMs = 150000, bufferBps = 300, log = () => {}, transfer = sendUsdc }) {
  const evmKey = generatePrivateKey();
  const address = privateKeyToAccount(evmKey).address;
  const need = await oneClick.quoteUsdcOnBase({ usdcOut: usdc, recipient: address, refundTo: accountId, dry: true });
  const amountIn = (BigInt(need.amountIn) * BigInt(10000 + bufferBps)) / 10000n;
  const quote = await oneClick.quoteToBaseUsdc({ amountIn, recipient: address, refundTo: accountId, dry: false });
  const record = { address, privateKey: evmKey, created: new Date().toISOString(), deposit_address: quote.depositAddress, cost_usdc: quote.amountInFormatted };
  const save = () => {
    if (!walletDir) return;
    mkdirSync(walletDir, { recursive: true });
    writeFileSync(`${walletDir}/${address}.json`, JSON.stringify(record), { mode: 0o600 });
  };
  save();
  log(`quote: ${quote.amountInFormatted} USDC on NEAR -> about ${quote.amountOutFormatted} USDC on Base, deposit ${quote.depositAddress}`);
  record.deposit_tx = await transfer({ accountId, privateKey, rpcUrl, to: quote.depositAddress, amount: quote.amountIn });
  save();
  log(`deposit sent: ${record.deposit_tx}`);
  if (record.deposit_tx) await oneClick.submitDeposit(quote.depositAddress, record.deposit_tx).catch(() => {});

  const deadline = Date.now() + timeoutMs;
  let status = null;
  while (Date.now() < deadline) {
    status = await oneClick.status(quote.depositAddress).catch(() => null);
    if (status && TERMINAL.has(status.status)) break;
    await new Promise((r) => setTimeout(r, pollMs));
  }
  const details = status?.swapDetails || {};
  return {
    evmKey,
    address,
    ok: status?.status === 'SUCCESS',
    intents_status: TERMINAL.has(status?.status) ? status.status : 'PENDING',
    deposit_address: quote.depositAddress,
    deposit_tx: record.deposit_tx ?? details.originChainTxHashes?.[0]?.hash ?? null,
    funded_with: 'USDC on NEAR',
    cost_usdc: quote.amountInFormatted,
    delivered_usdc: details.amountOutFormatted ?? null,
    base_tx: details.destinationChainTxHashes?.[0]?.hash ?? null,
  };
}

export async function payBase(url, { evmKey, maxUsdc, method = 'GET', body, fetchImpl = globalThis.fetch } = {}) {
  const account = privateKeyToAccount(evmKey);
  const pickBase = (_v, reqs) => {
    const r = reqs.find((q) => q.network === BASE_MAINNET && q.scheme === 'exact');
    if (!r) throw new Error('no exact option on Base');
    return r;
  };
  const client = x402Client.fromConfig({
    schemes: [],
    paymentRequirementsSelector: pickBase,
    spendControls: { maxAmountPerPayment: `$${maxUsdc}`, allowedAssets: [{ network: BASE_MAINNET, asset: BASE_USDC }] },
  });
  registerExactEvmScheme(client, { signer: account, networks: [BASE_MAINNET], paymentRequirementsSelector: pickBase });
  const started = Date.now();
  return outcome(await wrapFetchWithPayment(fetchImpl, client)(url, init(method, body)), started);
}
