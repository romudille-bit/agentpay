/**
 * eip3009.test.mjs — node --test suite for the in-MCP EIP-3009 signer (AGE-40).
 *
 * The signature vector below is cross-checked against the Python SDK's signer
 * (x402[evm] ExactEvmScheme via eth_account) — same key, same authorization,
 * byte-identical digest and signature. Regenerate with:
 *   venv/bin/python npm/test/gen_vector.py
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  privateKeyToAddress,
  toChecksumAddress,
  eip712Digest,
  signDigest,
  buildPaymentSignature,
} from '../bin/eip3009.js';

// Well-known throwaway test key (hardhat account #0 — NEVER fund on mainnet).
const TEST_KEY = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80';
const TEST_ADDR = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266';

const BASE_USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
const DOMAIN = { name: 'USD Coin', version: '2', chainId: 8453, verifyingContract: BASE_USDC };

// Fixed authorization → deterministic digest + signature (cross-checked vs Python).
const AUTH = {
  from: TEST_ADDR,
  to: '0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7',
  value: '10000',
  validAfter: '1700000000',
  validBefore: '1700000300',
  nonce: '0x' + '11'.repeat(32),
};

test('privateKeyToAddress derives the checksummed address', () => {
  assert.equal(privateKeyToAddress(TEST_KEY), TEST_ADDR);
  assert.equal(privateKeyToAddress(TEST_KEY.slice(2)), TEST_ADDR); // 0x optional
});

test('toChecksumAddress matches EIP-55', () => {
  assert.equal(
    toChecksumAddress('0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'),
    BASE_USDC,
  );
});

test('EIP-712 digest matches the Python x402 signer', () => {
  const digest = Buffer.from(eip712Digest(DOMAIN, AUTH)).toString('hex');
  // Vector from gen_vector.py (eth_account.sign_typed_data over identical input)
  assert.equal('0x' + digest, VECTOR.digest);
});

test('signature matches the Python x402 signer byte-for-byte', () => {
  const digest = eip712Digest(DOMAIN, AUTH);
  assert.equal(signDigest(digest, TEST_KEY), VECTOR.signature);
});

test('buildPaymentSignature produces a decodable x402 v2 PaymentPayload', () => {
  const baseOpt = {
    scheme: 'exact',
    network: 'eip155:8453',
    amount_atomic: '10000',
    amount_usdc: '0.01',
    asset: BASE_USDC,
    pay_to: '0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7',
    maxTimeoutSeconds: 300,
  };
  const url = 'https://agentpay.tools/tools/verified_route/call';
  const { header, authorization } = buildPaymentSignature(baseOpt, url, TEST_KEY, TEST_ADDR);

  const decoded = JSON.parse(Buffer.from(header, 'base64').toString());
  assert.equal(decoded.x402Version, 2);
  assert.equal(decoded.payload.authorization.from, TEST_ADDR);
  assert.equal(decoded.payload.authorization.to, baseOpt.pay_to);
  assert.equal(decoded.payload.authorization.value, '10000');
  assert.match(decoded.payload.authorization.nonce, /^0x[0-9a-f]{64}$/);
  assert.match(decoded.payload.signature, /^0x[0-9a-f]{130}$/);
  assert.equal(decoded.resource.url, url);
  assert.equal(decoded.accepted.payTo, baseOpt.pay_to);
  assert.equal(decoded.accepted.network, 'eip155:8453');
  assert.equal(decoded.accepted.extra.name, 'USD Coin');
  assert.equal(decoded.accepted.extra.version, '2');
  assert.equal(decoded.accepted.extra.assetTransferMethod, 'eip3009');

  // Validity window: after < now < before, ~600s skew buffer
  const now = Math.floor(Date.now() / 1000);
  assert.ok(parseInt(authorization.validAfter, 10) <= now - 590);
  assert.ok(parseInt(authorization.validBefore, 10) >= now + 290);

  // The freshly-signed payload must verify against its own digest
  const digest = eip712Digest(DOMAIN, decoded.payload.authorization);
  assert.equal(signDigestRecovers(decoded.payload.signature, digest), TEST_ADDR.toLowerCase());
});

test('honors explicit extra from the 402 option (domain override)', () => {
  const baseOpt = {
    amount: '5000',
    asset: BASE_USDC,
    payTo: '0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7',
    network: 'eip155:8453',
    extra: { name: 'USDC', version: '1', assetTransferMethod: 'eip3009' },
  };
  const { header } = buildPaymentSignature(baseOpt, 'https://x/y', TEST_KEY, TEST_ADDR);
  const decoded = JSON.parse(Buffer.from(header, 'base64').toString());
  assert.equal(decoded.accepted.extra.name, 'USDC');
  assert.equal(decoded.accepted.amount, '5000');
});

// ── helpers ───────────────────────────────────────────────────────────────────

import { secp256k1 } from '@noble/curves/secp256k1';
import { keccak_256 } from '@noble/hashes/sha3';

/** Recover the signer address from a 65-byte r‖s‖v signature. */
function signDigestRecovers(sigHex, digest) {
  const raw = Buffer.from(sigHex.slice(2), 'hex');
  const sig = secp256k1.Signature.fromCompact(raw.subarray(0, 64))
    .addRecoveryBit(raw[64] - 27);
  const pub = sig.recoverPublicKey(digest).toRawBytes(false);
  return '0x' + Buffer.from(keccak_256(pub.slice(1)).slice(12)).toString('hex');
}

// ── cross-check vector (generated by npm/test/gen_vector.py) ─────────────────
const VECTOR = {
  digest: '0x447a774ce92c4dde3616d8b57984fdfa5909b77ffff5c4c893b95d87a172dff9',
  signature: '0x0dda696074eb86d2e8ad73bd01d02ee95b35dbc18012c5a7cdd709ae0cc1da462990e5e55522affac302378db7be6f3b23e87164d22f57b0a13c1b6d09739df11b',
};
