/**
 * stacks_tx.js — minimal Stacks signer for the x402 sBTC path (port of
 * agentpay/_stacks_tx.py, fixture-tested against it byte for byte).
 *
 * Builds and signs exactly one thing: a SIP-010 `sbtc-token::transfer` with
 * the payment_id in the memo and a deny-mode "exactly N sats leave sender"
 * post-condition, standard or sponsored (fee 0, relay co-signs). No I/O.
 * Specs: SIP-005, SIP-010. Rationale: docs/stacks-adapter.md.
 */

import { sha256 } from '@noble/hashes/sha256';
import { sha512_256 } from '@noble/hashes/sha512';
import { ripemd160 } from '@noble/hashes/ripemd160';
import { secp256k1 } from '@noble/curves/secp256k1';

export const STACKS_MAINNET_CAIP2 = 'stacks:1';
export const STACKS_TESTNET_CAIP2 = 'stacks:2147483648';
export const SBTC_CONTRACT = {
  mainnet: 'SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token',
  testnet: 'SN3VMHXEN64ZZF71JQ5VESXDWTR301XTTXGF4J8F1.sbtc-token',
};
const SBTC_ASSET_NAME = 'sbtc-token';

const TX_VERSION = { mainnet: 0x00, testnet: 0x80 };
const CHAIN_ID = { mainnet: 0x00000001, testnet: 0x80000000 };
const ADDR_VERSION = { mainnet: 22, testnet: 26 };
const AUTH_STANDARD = 0x04, AUTH_SPONSORED = 0x05;
const HASH_MODE_P2PKH = 0x00;
const KEY_COMPRESSED = 0x00, KEY_UNCOMPRESSED = 0x01;
const ANCHOR_ANY = 0x03, PC_MODE_DENY = 0x02;
const PC_FUNGIBLE = 0x01, PC_PRINCIPAL_STANDARD = 0x02, FT_SENT_EQ = 0x01;
const PAYLOAD_CONTRACT_CALL = 0x02;
const CV_UINT = 0x01, CV_BUFFER = 0x02, CV_PRINCIPAL = 0x05, CV_SOME = 0x0a;
const MEMO_MAX = 34;
const CONDITION_LEN = 1 + 20 + 8 + 8 + 1 + 65;
const ORIGIN_OFFSET = 6;
const SIG_PLACEHOLDER = new Uint8Array(65);
const SECP256K1_N = BigInt('0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141');

// ── bytes ────────────────────────────────────────────────────────────────────

export function hexToBytes(hex) {
  const h = hex.startsWith('0x') ? hex.slice(2) : hex;
  if (h.length % 2 || !/^[0-9a-fA-F]*$/.test(h)) throw new Error('invalid hex');
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(i * 2, i * 2 + 2), 16);
  return out;
}
export const bytesToHex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
function concat(...arrays) {
  const out = new Uint8Array(arrays.reduce((n, a) => n + a.length, 0));
  let off = 0;
  for (const a of arrays) { out.set(a, off); off += a.length; }
  return out;
}
function uintBE(value, len) {
  let v = BigInt(value);
  if (v < 0n || v >= 1n << BigInt(8 * len)) throw new Error('integer out of range');
  const out = new Uint8Array(len);
  for (let i = len - 1; i >= 0; i--) { out[i] = Number(v & 0xffn); v >>= 8n; }
  return out;
}
const u8 = (n) => new Uint8Array([n]);
const hash160 = (data) => ripemd160(sha256(data));
const textBytes = (s) => new TextEncoder().encode(s);

// ── c32check ─────────────────────────────────────────────────────────────────

const C32 = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const c32Normalize = (s) => s.toUpperCase().replace(/O/g, '0').replace(/[LI]/g, '1');

function c32Encode(data) {
  let num = BigInt('0x' + (bytesToHex(data) || '0'));
  let digits = '';
  while (num > 0n) { digits = C32[Number(num % 32n)] + digits; num /= 32n; }
  let zeros = 0;
  while (zeros < data.length && data[zeros] === 0) zeros++;
  return '0'.repeat(zeros) + digits;
}

function c32Checksum(version, data) {
  return sha256(sha256(concat(u8(version), data))).slice(0, 4);
}

export function c32Address(version, h160) {
  if (h160.length !== 20) throw new Error('hash160 must be 20 bytes');
  return 'S' + C32[version] + c32Encode(concat(h160, c32Checksum(version, h160)));
}

export function c32Decode(address) {
  if (typeof address !== 'string' || address.length < 6 || address[0].toUpperCase() !== 'S') {
    throw new Error('invalid c32 address');
  }
  const norm = c32Normalize(address);
  const version = C32.indexOf(norm[1]);
  if (version < 0) throw new Error('invalid c32 address version');
  let num = 0n;
  for (const ch of norm.slice(2)) {
    const d = C32.indexOf(ch);
    if (d < 0) throw new Error('invalid c32 address');
    num = num * 32n + BigInt(d);
  }
  const payload = uintBE(num, 24); // 20-byte hash160 + 4-byte checksum, left-padded
  const data = payload.slice(0, 20), checksum = payload.slice(20);
  if (bytesToHex(c32Checksum(version, data)) !== bytesToHex(checksum)) {
    throw new Error('invalid c32 address checksum');
  }
  return { version, hash160: data };
}

// ── keys ─────────────────────────────────────────────────────────────────────

/** 64-hex raw → uncompressed, 66-hex ending "01" → compressed (stacks.js parity). */
export function keypairFromSecret(secretHex) {
  const s = String(secretHex || '').trim().toLowerCase().replace(/^0x/, '');
  let raw, compressed;
  if (s.length === 66 && s.endsWith('01')) { raw = s.slice(0, 64); compressed = true; }
  else if (s.length === 64) { raw = s; compressed = false; }
  else throw new Error('invalid Stacks private key');
  if (!/^[0-9a-f]{64}$/.test(raw)) throw new Error('invalid Stacks private key');
  const d = BigInt('0x' + raw);
  if (d <= 0n || d >= SECP256K1_N) throw new Error('invalid Stacks private key');
  const privateKey = hexToBytes(raw);
  const publicKey = secp256k1.getPublicKey(privateKey, compressed);
  const signer = hash160(publicKey);
  return {
    privateKey, compressed, publicKey, signer,
    address: (network) => c32Address(ADDR_VERSION[network], signer),
  };
}

// ── serialization ────────────────────────────────────────────────────────────

function clarityName(name) {
  const b = textBytes(name);
  if (!b.length || b.length > 128) throw new Error('invalid Clarity name');
  return concat(u8(b.length), b);
}
function serializeAddress(address) {
  const { version, hash160: h } = c32Decode(address);
  return concat(u8(version), h);
}
function splitContract(id) {
  const parts = String(id).split('.');
  if (parts.length !== 2) throw new Error("contract id must be 'address.contract-name'");
  return parts;
}
const cvUint = (v) => concat(u8(CV_UINT), uintBE(v, 16));
const cvPrincipal = (addr) => concat(u8(CV_PRINCIPAL), serializeAddress(addr));
const cvBuffer = (b) => concat(u8(CV_BUFFER), uintBE(b.length, 4), b);
const cvSome = (inner) => concat(u8(CV_SOME), inner);

function spendingCondition(signer, nonce, fee, keyEncoding, signature) {
  return concat(u8(HASH_MODE_P2PKH), signer, uintBE(nonce, 8), uintBE(fee, 8),
                u8(keyEncoding), signature);
}
// Sponsor placeholder (serialized) vs cleared (sighash) forms, stacks.js parity.
const SPONSOR_PLACEHOLDER = spendingCondition(hash160(new Uint8Array(33)), 0, 0,
                                              KEY_COMPRESSED, SIG_PLACEHOLDER);
const SPONSOR_CLEARED = spendingCondition(new Uint8Array(20), 0, 0, KEY_COMPRESSED, SIG_PLACEHOLDER);

function postCondition(sender, contract, amountSats) {
  const [addr, name] = splitContract(contract);
  return concat(u8(PC_FUNGIBLE), u8(PC_PRINCIPAL_STANDARD), serializeAddress(sender),
                serializeAddress(addr), clarityName(name), clarityName(SBTC_ASSET_NAME),
                u8(FT_SENT_EQ), uintBE(amountSats, 8));
}

/** Unsigned `sbtc-token::transfer`: memo = payment_id, deny-mode exact post-condition. */
export function buildSbtcTransfer({ sender, recipient, amountSats, paymentId, nonce,
                                    feeMicrostx, network = 'testnet', sponsored = false,
                                    contract = null }) {
  if (!(network in TX_VERSION)) throw new Error("network must be 'mainnet' or 'testnet'");
  if (!(Number(amountSats) > 0)) throw new Error('amountSats must be positive');
  if (!paymentId) throw new Error('paymentId is required (challenge binding)');
  if (Number(nonce) < 0 || Number(feeMicrostx) < 0) throw new Error('nonce and fee must be non-negative');
  const memo = textBytes(paymentId).slice(0, MEMO_MAX);
  const contractId = contract || SBTC_CONTRACT[network];
  const [cAddr, cName] = splitContract(contractId);
  const senderAddress = sender.address(network);
  const keyEncoding = sender.compressed ? KEY_COMPRESSED : KEY_UNCOMPRESSED;
  const payload = concat(
    u8(PAYLOAD_CONTRACT_CALL), serializeAddress(cAddr), clarityName(cName), clarityName('transfer'),
    uintBE(4, 4), cvUint(amountSats), cvPrincipal(senderAddress), cvPrincipal(recipient),
    cvSome(cvBuffer(memo)),
  );
  return concat(
    u8(TX_VERSION[network]), uintBE(CHAIN_ID[network], 4),
    u8(sponsored ? AUTH_SPONSORED : AUTH_STANDARD),
    spendingCondition(sender.signer, nonce, feeMicrostx, keyEncoding, SIG_PLACEHOLDER),
    sponsored ? SPONSOR_PLACEHOLDER : new Uint8Array(0),
    u8(ANCHOR_ANY), u8(PC_MODE_DENY), uintBE(1, 4),
    postCondition(senderAddress, contractId, amountSats),
    payload,
  );
}

// ── signing (SIP-005 presign sighash) ────────────────────────────────────────

function originFields(tx) {
  const o = ORIGIN_OFFSET;
  if (tx.length < o + CONDITION_LEN || tx[o] !== HASH_MODE_P2PKH) throw new Error('malformed Stacks transaction');
  const be = (b) => BigInt('0x' + bytesToHex(b));
  return { nonce: be(tx.slice(o + 21, o + 29)), fee: be(tx.slice(o + 29, o + 37)), keyEncoding: tx[o + 37] };
}
const withOrigin = (tx, cond) => concat(tx.slice(0, ORIGIN_OFFSET), cond, tx.slice(ORIGIN_OFFSET + CONDITION_LEN));

function presignHash(tx, signer, keyEncoding) {
  const { nonce, fee } = originFields(tx);
  let cleared = withOrigin(tx, spendingCondition(signer, 0, 0, keyEncoding, SIG_PLACEHOLDER));
  if (tx[5] === AUTH_SPONSORED) {
    const o = ORIGIN_OFFSET + CONDITION_LEN;
    cleared = concat(cleared.slice(0, o), SPONSOR_CLEARED, cleared.slice(o + CONDITION_LEN));
  }
  // The origin signs with the standard auth byte even inside a sponsored tx.
  return sha512_256(concat(sha512_256(cleared), u8(AUTH_STANDARD), uintBE(fee, 8), uintBE(nonce, 8)));
}

export function signTransaction(unsignedTx, keypair) {
  if (unsignedTx[5] !== AUTH_STANDARD && unsignedTx[5] !== AUTH_SPONSORED) throw new Error('malformed Stacks transaction');
  const { nonce, fee } = originFields(unsignedTx);
  if (bytesToHex(unsignedTx.slice(7, 27)) !== bytesToHex(keypair.signer)) throw new Error('keypair does not match transaction signer');
  const keyEncoding = keypair.compressed ? KEY_COMPRESSED : KEY_UNCOMPRESSED;
  const sig = secp256k1.sign(presignHash(unsignedTx, keypair.signer, keyEncoding), keypair.privateKey);
  const signature = concat(u8(sig.recovery), uintBE(sig.r, 32), uintBE(sig.s, 32));
  return withOrigin(unsignedTx, spendingCondition(keypair.signer, nonce, fee, keyEncoding, signature));
}

/** sha512/256 of the signed bytes; for a sponsored tx the relay's txid differs. */
export const txidOf = (signedTx) => bytesToHex(sha512_256(signedTx));
export const isSponsored = (tx) => tx.length > 5 && tx[5] === AUTH_SPONSORED;

// ── cap binding (mirrors _stacks_tx.assert_sats_within_cap) ──────────────────

const MIN_BTC_USD = { mainnet: 20000, testnet: 10000 };

export function satsFromUsd(amountUsd, btcUsdRate) {
  if (!(btcUsdRate > 0)) throw new Error('btc_usd_rate must be positive');
  return Math.ceil((amountUsd / btcUsdRate) * 1e8 - 1e-9);
}

/** Refuse sats the USD cap doesn't bound: a floor-rate ceiling, then ≤2% drift vs the quoted rate. */
export function assertSatsWithinCap(amountSats, amountUsd, btcUsdRate, network = 'testnet', env = process.env) {
  const usd = parseFloat(amountUsd);
  if (!Number.isFinite(usd) || usd < 0) throw new Error('no amount_usdc to bound amount_sats against');
  const dflt = MIN_BTC_USD[network] ?? MIN_BTC_USD.testnet;
  const floor = Math.max(parseFloat(env.STACKS_MIN_BTC_USD) || dflt, dflt);
  const maxSats = satsFromUsd(usd, floor);
  if (amountSats > maxSats) {
    throw new Error(`amount_sats=${amountSats} exceeds the most sats $${usd} could buy at the $${floor}/BTC floor (${maxSats}) — refusing to sign`);
  }
  if (btcUsdRate != null) {
    const rate = parseFloat(btcUsdRate);
    if (!(rate > 0)) throw new Error(`402 quotes a non-positive BTC/USD rate (${btcUsdRate})`);
    const expected = satsFromUsd(usd, rate);
    const tol = Math.min(parseFloat(env.STACKS_SATS_TOLERANCE) || 0.02, 0.02);
    const slack = Math.max(expected * tol, 2);
    if (Math.abs(amountSats - expected) > slack) {
      throw new Error(`amount_sats=${amountSats} inconsistent with $${usd} at ${rate} BTC/USD (expected ~${expected}) — refusing to sign`);
    }
  }
}

// ── the payment-signature header (mirrors AgentWallet.build_stacks_payment) ─

/**
 * Sign a 402's `payment_options.stacks` offer. `sponsored` defaults to what the
 * offer advertises (`sponsorship.available`); forcing it on without an offer throws.
 * Returns { header, txid, nonce, amountSats, amountUsd, sponsored, sender, signedTx }.
 */
export function buildStacksPayment(stacksOpt, paymentId, resourceUrl, keypair, network,
                                   nonce, { sponsored = null, env = process.env } = {}) {
  const caip2 = network === 'mainnet' ? STACKS_MAINNET_CAIP2 : STACKS_TESTNET_CAIP2;
  const offeredNet = stacksOpt.network || caip2;
  if (offeredNet !== caip2) throw new Error(`402 stacks option targets ${offeredNet} but this wallet is on ${caip2} — refusing to sign`);
  const amountSats = Number(stacksOpt.amount_sats);
  if (!Number.isInteger(amountSats) || amountSats <= 0) throw new Error('402 stacks option has an unreadable amount_sats');
  assertSatsWithinCap(amountSats, stacksOpt.amount_usdc, stacksOpt.btc_usd_rate, network, env);
  const payTo = stacksOpt.pay_to || stacksOpt.payTo;
  if (!payTo) throw new Error('402 stacks option has no pay_to address');

  const offered = Boolean(stacksOpt.sponsorship?.available);
  if (sponsored === true && !offered) throw new Error('sponsored Stacks payment required but the 402 offers no sponsorship');
  const useSponsored = sponsored === null ? offered : Boolean(sponsored);
  let fee = 0;
  if (!useSponsored) {
    fee = Number(stacksOpt.fee_microstx || env.STACKS_FEE_MICROSTX || 3000);
    const cap = Math.min(Number(env.STACKS_MAX_FEE_MICROSTX) || 50_000, 50_000);
    if (fee > cap) fee = cap;
    if (!(fee >= 1)) throw new Error('fee_microstx must be positive');
  }
  const unsigned = buildSbtcTransfer({ sender: keypair, recipient: payTo, amountSats, paymentId,
                                       nonce, feeMicrostx: fee, network, sponsored: useSponsored });
  const signedTx = signTransaction(unsigned, keypair);
  const txid = txidOf(signedTx);
  const payload = {
    x402Version: 2,
    scheme: stacksOpt.scheme || 'exact',
    network: caip2,
    payment_id: paymentId,
    payload: { signedTransaction: bytesToHex(signedTx), txid, sponsored: useSponsored },
    accepted: { scheme: stacksOpt.scheme || 'exact', network: caip2, amount: String(amountSats),
                asset: 'sbtc', payTo, resource: resourceUrl, mimeType: 'application/json' },
  };
  return {
    header: Buffer.from(JSON.stringify(payload)).toString('base64'),
    txid, nonce, amountSats, amountUsd: stacksOpt.amount_usdc,
    sponsored: useSponsored, sender: keypair.address(network), signedTx,
  };
}
