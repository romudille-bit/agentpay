/**
 * wallet.test.mjs — AGE-139 step 1: the four wallet/paid-mode states, plus
 * the failure modes that must never lose a key.
 *
 *   1. no key, no file        → mint + persist (0600), paid OFF
 *   2. persisted file exists  → same address every start, paid OFF
 *   3. BYO AGENTPAY_BASE_KEY  → that address, paid ON, disk untouched
 *   4. persisted + ENABLE_PAID→ persisted address, paid ON
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { defaultWalletPath, loadOrCreateWallet, paidModeEnabled } from '../bin/wallet.js';
import { privateKeyToAddress } from '../bin/eip3009.js';

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'agentpay-wallet-'));
const envWith = (dir, extra = {}) => ({ AGENTPAY_WALLET_PATH: path.join(dir, 'mcp-wallet.json'), ...extra });

const BYO_KEY = '0x' + '11'.repeat(32);

test('state 1: first run mints, persists with tight modes, paid stays off', () => {
  const dir = tmp();
  const env = envWith(dir);
  const w = loadOrCreateWallet(env);
  assert.equal(w.source, 'minted');
  assert.match(w.address, /^0x[0-9a-fA-F]{40}$/);
  const file = JSON.parse(fs.readFileSync(env.AGENTPAY_WALLET_PATH, 'utf8'));
  assert.equal(file.address, w.address);
  assert.equal(privateKeyToAddress(file.key), w.address);
  assert.ok(file.created_at && file.version >= 1);
  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(env.AGENTPAY_WALLET_PATH).mode & 0o777, 0o600);
  }
  assert.equal(paidModeEnabled(env, w.source), false);
});

test('state 2: second start loads the SAME address (the whole point)', () => {
  const dir = tmp();
  const env = envWith(dir);
  const first = loadOrCreateWallet(env);
  const second = loadOrCreateWallet(env);
  assert.equal(second.source, 'file');
  assert.equal(second.address, first.address);
  assert.equal(paidModeEnabled(env, second.source), false);
});

test('state 3: BYO key wins, implies paid mode, never touches disk', () => {
  const dir = tmp();
  const env = envWith(dir, { AGENTPAY_BASE_KEY: BYO_KEY });
  const w = loadOrCreateWallet(env);
  assert.equal(w.source, 'env');
  assert.equal(w.address, privateKeyToAddress(BYO_KEY));
  assert.equal(fs.existsSync(env.AGENTPAY_WALLET_PATH), false);
  assert.equal(paidModeEnabled(env, w.source), true);
});

test('state 4: persisted wallet + AGENTPAY_ENABLE_PAID=1 → paid mode on', () => {
  const dir = tmp();
  const env = envWith(dir, { AGENTPAY_ENABLE_PAID: '1' });
  const w = loadOrCreateWallet(env);
  assert.equal(w.source, 'minted');
  assert.equal(paidModeEnabled(env, w.source), true);
  for (const v of ['true', 'YES']) {
    assert.equal(paidModeEnabled({ AGENTPAY_ENABLE_PAID: v }, 'file'), true);
  }
  for (const v of ['', '0', 'no', 'off']) {
    assert.equal(paidModeEnabled({ AGENTPAY_ENABLE_PAID: v }, 'file'), false);
  }
});

test('invalid BYO key falls back to the persisted wallet, not keyless', () => {
  const dir = tmp();
  const logs = [];
  const env = envWith(dir, { AGENTPAY_BASE_KEY: 'not-a-key' });
  const w = loadOrCreateWallet(env, (m) => logs.push(m));
  assert.equal(w.source, 'minted');
  assert.ok(logs.some((m) => m.includes('invalid AGENTPAY_BASE_KEY')));
  // and the invalid key does NOT imply paid mode
  assert.equal(paidModeEnabled(env, w.source), false);
});

test('corrupt wallet file is NEVER overwritten — ephemeral fallback', () => {
  const dir = tmp();
  const env = envWith(dir);
  fs.writeFileSync(env.AGENTPAY_WALLET_PATH, '{not json');
  const logs = [];
  const w = loadOrCreateWallet(env, (m) => logs.push(m));
  assert.equal(w.source, 'ephemeral');
  assert.equal(w.error, 'corrupt');
  assert.match(w.address, /^0x[0-9a-fA-F]{40}$/);
  assert.equal(fs.readFileSync(env.AGENTPAY_WALLET_PATH, 'utf8'), '{not json');
  assert.ok(logs.some((m) => m.includes('NOT overwriting')));
});

test('unwritable path → ephemeral wallet with a usable address', () => {
  const dir = tmp();
  fs.chmodSync(dir, 0o500);                       // no write
  const env = { AGENTPAY_WALLET_PATH: path.join(dir, 'sub', 'w.json') };
  try {
    const logs = [];
    const w = loadOrCreateWallet(env, (m) => logs.push(m));
    if (process.platform === 'win32' || process.getuid?.() === 0) return; // root writes anyway
    assert.equal(w.source, 'ephemeral');
    assert.equal(w.error, 'unwritable');
    assert.match(w.address, /^0x[0-9a-fA-F]{40}$/);
    assert.ok(logs.some((m) => m.includes('cannot write wallet file')));
  } finally {
    fs.chmodSync(dir, 0o700);
  }
});

test('defaultWalletPath: override wins, else ~/.agentpay/mcp-wallet.json', () => {
  assert.equal(defaultWalletPath({ AGENTPAY_WALLET_PATH: '/x/y.json' }), '/x/y.json');
  assert.equal(defaultWalletPath({}, () => '/home/u'),
               path.join('/home/u', '.agentpay', 'mcp-wallet.json'));
});
