/**
 * eip3009.js — minimal EIP-3009 transferWithAuthorization signer (Mode A, x402 v2).
 *
 * Ports agentpay/_wallet.py:build_base_payment_signature to Node, using
 * @noble/curves + @noble/hashes (no viem, no Python — keeps the MCP light).
 *
 * Contract (must match gateway/base.py + the x402 python lib exactly):
 *   - EIP-712 domain: { name: extra.name, version: extra.version,
 *                       chainId: <from CAIP-2 network>, verifyingContract: asset }
 *   - Primary type: TransferWithAuthorization(address from, address to,
 *       uint256 value, uint256 validAfter, uint256 validBefore, bytes32 nonce)
 *   - Header: PAYMENT-SIGNATURE: base64(JSON PaymentPayload)
 *     { x402Version: 2, payload: { authorization, signature }, resource, accepted }
 *
 * NOTHING is broadcast client-side — the gateway's CDP facilitator settles the
 * signed authorization only if it accepts the retry, so a rejected call moves
 * no USDC (gasless x402 v2 flow, same as the Python SDK).
 */

import { randomBytes } from 'crypto';
import { keccak_256 } from '@noble/hashes/sha3';
import { secp256k1 } from '@noble/curves/secp256k1';

const BASE_USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';

// ── Hex / bytes helpers ───────────────────────────────────────────────────────

function hexToBytes(hex) {
  const h = hex.startsWith('0x') ? hex.slice(2) : hex;
  if (h.length % 2 !== 0) throw new Error(`odd-length hex: ${hex.slice(0, 12)}…`);
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function bytesToHex(bytes) {
  return '0x' + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

function concat(...arrays) {
  const len = arrays.reduce((n, a) => n + a.length, 0);
  const out = new Uint8Array(len);
  let off = 0;
  for (const a of arrays) { out.set(a, off); off += a.length; }
  return out;
}

// ── ABI encoding (the 3 fixed types EIP-3009 needs) ──────────────────────────

/** address → 32-byte left-padded word */
function encAddress(addr) {
  const b = hexToBytes(addr);
  if (b.length !== 20) throw new Error(`invalid address length: ${addr}`);
  return concat(new Uint8Array(12), b);
}

/** uint256 (BigInt/number/decimal string) → 32-byte big-endian word */
function encUint256(value) {
  let v = BigInt(value);
  if (v < 0n) throw new Error(`uint256 cannot be negative: ${value}`);
  const out = new Uint8Array(32);
  for (let i = 31; i >= 0 && v > 0n; i--) { out[i] = Number(v & 0xffn); v >>= 8n; }
  return out;
}

/** bytes32 hex → 32-byte word */
function encBytes32(hex) {
  const b = hexToBytes(hex);
  if (b.length !== 32) throw new Error(`invalid bytes32 length: ${hex.slice(0, 12)}…`);
  return b;
}

// ── EIP-712 ───────────────────────────────────────────────────────────────────

const DOMAIN_TYPEHASH = keccak_256(new TextEncoder().encode(
  'EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)',
));
const TRANSFER_TYPEHASH = keccak_256(new TextEncoder().encode(
  'TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)',
));

function domainSeparator({ name, version, chainId, verifyingContract }) {
  return keccak_256(concat(
    DOMAIN_TYPEHASH,
    keccak_256(new TextEncoder().encode(name)),
    keccak_256(new TextEncoder().encode(version)),
    encUint256(chainId),
    encAddress(verifyingContract),
  ));
}

function structHash(auth) {
  return keccak_256(concat(
    TRANSFER_TYPEHASH,
    encAddress(auth.from),
    encAddress(auth.to),
    encUint256(auth.value),
    encUint256(auth.validAfter),
    encUint256(auth.validBefore),
    encBytes32(auth.nonce),
  ));
}

/** EIP-712 digest: keccak(0x1901 ‖ domainSeparator ‖ structHash) */
export function eip712Digest(domain, auth) {
  return keccak_256(concat(
    new Uint8Array([0x19, 0x01]),
    domainSeparator(domain),
    structHash(auth),
  ));
}

// ── Key / address ─────────────────────────────────────────────────────────────

/** EIP-55 checksummed address from a raw 20-byte address hex */
export function toChecksumAddress(addr) {
  const lower = (addr.startsWith('0x') ? addr.slice(2) : addr).toLowerCase();
  const hash = Array.from(keccak_256(new TextEncoder().encode(lower)),
    (b) => b.toString(16).padStart(2, '0')).join('');
  let out = '0x';
  for (let i = 0; i < lower.length; i++) {
    out += parseInt(hash[i], 16) >= 8 ? lower[i].toUpperCase() : lower[i];
  }
  return out;
}

/** Derive the EIP-55 address from a secp256k1 private key (hex, 0x optional). */
export function privateKeyToAddress(privKeyHex) {
  const priv = hexToBytes(privKeyHex);
  if (priv.length !== 32) throw new Error('AGENTPAY_BASE_KEY must be a 32-byte hex private key');
  const pub = secp256k1.getPublicKey(priv, false); // uncompressed, 65 bytes
  const hash = keccak_256(pub.slice(1));           // drop 0x04 prefix
  return toChecksumAddress(bytesToHex(hash.slice(12)));
}

/** Sign an EIP-712 digest → 65-byte r‖s‖v signature hex (v = 27/28). */
export function signDigest(digest, privKeyHex) {
  const priv = hexToBytes(privKeyHex);
  const sig = secp256k1.sign(digest, priv); // lowS by default (Ethereum requires it)
  const rs = sig.toCompactRawBytes();       // 64 bytes r‖s
  const v = new Uint8Array([sig.recovery + 27]);
  return bytesToHex(concat(rs, v));
}

// ── PaymentPayload builder (mirrors _wallet.build_base_payment_signature) ────

/**
 * Build the base64 PAYMENT-SIGNATURE header for an AgentPay 402
 * `payment_options.base` block.
 *
 * @param {object} baseOpt      payment_options.base from the 402 body
 * @param {string} resourceUrl  the tool URL being paid for
 * @param {string} privKeyHex   EVM private key (hex)
 * @param {string} fromAddress  checksummed address of privKeyHex
 * @returns {{ header: string, authorization: object }}
 */
export function buildPaymentSignature(baseOpt, resourceUrl, privKeyHex, fromAddress) {
  const amount = String(baseOpt.amount_atomic ?? baseOpt.amount);
  const asset = baseOpt.asset || BASE_USDC;
  const payTo = baseOpt.pay_to || baseOpt.payTo;
  const network = baseOpt.network || 'eip155:8453';
  const scheme = baseOpt.scheme || 'exact';
  const timeout = parseInt(baseOpt.maxTimeoutSeconds ?? 300, 10);
  const extra = baseOpt.extra || {
    name: 'USD Coin', version: '2', assetTransferMethod: 'eip3009',
  };
  if (!payTo) throw new Error('402 Base option is missing pay_to');
  if (!network.startsWith('eip155:')) throw new Error(`unsupported network: ${network}`);
  const chainId = parseInt(network.split(':')[1], 10);

  const now = Math.floor(Date.now() / 1000);
  const authorization = {
    from: fromAddress,
    to: payTo,
    value: amount,
    validAfter: String(now - 600),          // clock-skew buffer (matches x402 lib)
    validBefore: String(now + timeout),
    nonce: bytesToHex(randomBytes(32)),
  };

  const digest = eip712Digest(
    { name: extra.name, version: extra.version || '1', chainId, verifyingContract: asset },
    authorization,
  );
  const signature = signDigest(digest, privKeyHex);

  const paymentPayload = {
    x402Version: 2,
    payload: { authorization, signature },
    resource: { url: resourceUrl, mimeType: 'application/json' },
    accepted: {
      scheme, network, amount, asset, payTo,
      maxTimeoutSeconds: timeout,
      resource: resourceUrl, mimeType: 'application/json',
      extra,
    },
  };
  const header = Buffer.from(JSON.stringify(paymentPayload)).toString('base64');
  return { header, authorization };
}
