/**
 * stacks_tx.test.mjs — the in-MCP Stacks signer against the Python SDK's
 * fixtures (tests/fixtures/stacks_tx_fixtures.json, themselves generated
 * from @stacks/transactions): same inputs, byte-identical signed tx + txid.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import {
  keypairFromSecret, c32Address, c32Decode, hexToBytes, bytesToHex,
  buildSbtcTransfer, signTransaction, txidOf, isSponsored,
  assertSatsWithinCap, buildStacksPayment, satsFromUsd,
} from '../bin/stacks_tx.js';

const here = dirname(fileURLToPath(import.meta.url));
const FIX = JSON.parse(readFileSync(join(here, '..', '..', 'tests', 'fixtures', 'stacks_tx_fixtures.json'), 'utf8'));

test('keys derive the fixture addresses and public keys', () => {
  for (const k of FIX.keys) {
    const kp = keypairFromSecret(k.private_key);
    assert.equal(bytesToHex(kp.publicKey), k.public_key);
    assert.equal(kp.address('mainnet'), k.address_mainnet);
    assert.equal(kp.address('testnet'), k.address_testnet);
  }
  assert.throws(() => keypairFromSecret('00'.repeat(32)), /invalid Stacks private key/);
  assert.throws(() => keypairFromSecret('abc'), /invalid Stacks private key/);
});

test('c32check round-trips the fixture vectors', () => {
  for (const v of FIX.c32_vectors) {
    assert.equal(c32Address(v.version, hexToBytes(v.hash160)), v.address);
    const d = c32Decode(v.address);
    assert.equal(d.version, v.version);
    assert.equal(bytesToHex(d.hash160), v.hash160);
  }
  assert.throws(() => c32Decode('SP000000000000000000002Q6VF79'), /checksum/);
});

test('signed transactions are byte-identical to the Python fixtures (incl. sponsored)', () => {
  let sponsoredSeen = false;
  for (const t of FIX.transactions) {
    const key = FIX.keys.find((k) => k.address_testnet === t.sender || k.address_mainnet === t.sender);
    const kp = keypairFromSecret(key.private_key);
    const unsigned = buildSbtcTransfer({
      sender: kp, recipient: t.recipient, amountSats: Number(t.amount_sats),
      paymentId: t.payment_id, nonce: Number(t.nonce), feeMicrostx: Number(t.fee),
      network: t.network, sponsored: t.sponsored, contract: t.contract,
    });
    const signed = signTransaction(unsigned, kp);
    assert.equal(bytesToHex(signed), t.serialized_hex, t.name);
    assert.equal(txidOf(signed), t.txid, t.name);
    assert.equal(isSponsored(signed), t.sponsored);
    sponsoredSeen ||= t.sponsored;
  }
  assert.ok(sponsoredSeen, 'fixtures include a sponsored tx');
});

test('cap binding refuses inflated sats and accepts the quote', () => {
  assert.equal(satsFromUsd(0.01, 100000), 10);
  assertSatsWithinCap(10, '0.01', '100000', 'testnet', {});
  assert.throws(() => assertSatsWithinCap(10_000, '0.01', '100000', 'testnet', {}), /exceeds the most sats/);
  assert.throws(() => assertSatsWithinCap(13, '0.01', '100000', 'testnet', {}), /inconsistent/);
  assertSatsWithinCap(12, '0.01', '100000', 'testnet', {}); // within the 2-sat slack
});

test('buildStacksPayment: sponsored when offered, own fee otherwise, forced needs the offer', () => {
  const kp = keypairFromSecret(FIX.keys[0].private_key);
  const base = { amount_sats: 10, amount_usdc: '0.01', btc_usd_rate: '100000',
                 pay_to: FIX.keys[1].address_testnet, network: 'stacks:2147483648', fee_microstx: 500 };
  const plain = buildStacksPayment(base, 'pay_1', 'https://g/tools/x/call', kp, 'testnet', 3);
  assert.equal(plain.sponsored, false);
  assert.equal(plain.signedTx[5], 0x04);
  const sponsored = buildStacksPayment({ ...base, sponsorship: { available: true } },
                                       'pay_1', 'https://g/tools/x/call', kp, 'testnet', 3);
  assert.equal(sponsored.sponsored, true);
  assert.equal(sponsored.signedTx[5], 0x05);
  assert.equal(bytesToHex(sponsored.signedTx.slice(35, 43)), '0000000000000000'); // fee 0
  const payload = JSON.parse(Buffer.from(sponsored.header, 'base64').toString());
  assert.equal(payload.payload.sponsored, true);
  assert.equal(payload.payment_id, 'pay_1');
  assert.equal(payload.accepted.payTo, base.pay_to);
  assert.throws(() => buildStacksPayment(base, 'pay_1', 'u', kp, 'testnet', 3, { sponsored: true }),
                /offers no sponsorship/);
  const optOut = buildStacksPayment({ ...base, sponsorship: { available: true } }, 'pay_1', 'u', kp,
                                    'testnet', 3, { sponsored: false });
  assert.equal(optOut.sponsored, false);
  assert.throws(() => buildStacksPayment({ ...base, network: 'stacks:1' }, 'pay_1', 'u', kp, 'testnet', 3),
                /refusing to sign/);
});
