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
import { NEAR } from 'near-api-js/tokens';
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';
import { BASE_MAINNET, NEAR_MAINNET } from './challenge.mjs';
import { createOneClick } from './oneclick.mjs';

export const NEAR_RPC = 'https://rpc.mainnet.fastnear.com';
export const BASE_RPC = 'https://mainnet.base.org';
export const BASE_USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
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

export async function payNear(url, { accountId, privateKey, method = 'GET', body, rpcUrl = NEAR_RPC, fetchImpl } = {}) {
  const signer = createLocalNearSigner({ accountId, secretKey: privateKey, rpcUrls: { [NEAR_MAINNET]: rpcUrl } });
  const paidFetch = createNearPaymentFetch({ signer, network: NEAR_MAINNET, ...(fetchImpl ? { fetch: fetchImpl } : {}) });
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

/** Top a fresh Base wallet up with about `usdc` by sending NEAR to a 1Click deposit
 *  address. The wallet file is written before any money moves, so a later run can
 *  pick a slow swap up. */
export async function fundBaseWallet({ accountId, privateKey, usdc, rpcUrl = NEAR_RPC, oneClick = createOneClick(), walletDir = null, pollMs = 5000, timeoutMs = 150000, bufferBps = 300, log = () => {} }) {
  const evmKey = generatePrivateKey();
  const address = privateKeyToAccount(evmKey).address;
  const need = await oneClick.quoteUsdcOnBase({ usdcOut: usdc, recipient: address, refundTo: accountId, dry: true });
  const nearIn = (BigInt(need.amountIn) * BigInt(10000 + bufferBps)) / 10000n;
  const quote = await oneClick.quoteNearToBaseUsdc({ nearIn, recipient: address, refundTo: accountId, dry: false });
  const record = { address, privateKey: evmKey, created: new Date().toISOString(), deposit_address: quote.depositAddress, cost_near: quote.amountInFormatted };
  const save = () => {
    if (!walletDir) return;
    mkdirSync(walletDir, { recursive: true });
    writeFileSync(`${walletDir}/${address}.json`, JSON.stringify(record), { mode: 0o600 });
  };
  save();
  log(`quote: ${quote.amountInFormatted} NEAR -> about ${quote.amountOutFormatted} USDC on Base, deposit ${quote.depositAddress}`);
  const account = new Account(accountId, new JsonRpcProvider({ url: rpcUrl }), privateKey);
  const sent = await account.transfer({ token: NEAR, amount: quote.amountIn, receiverId: quote.depositAddress });
  record.deposit_tx = sent?.transaction?.hash ?? null;
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
    deposit_tx: record.deposit_tx,
    cost_near: quote.amountInFormatted,
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
