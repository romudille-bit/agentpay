/**
 * wallet.js — persisted MCP wallet identity (AGE-139 step 1).
 *
 * The keyless MCP used to introduce itself as `mcp-free-<uuid>`, regenerated
 * on every process start — 26 paid-tool reaches could be 26 people or one
 * person restarting Claude Desktop 26 times, and retention was unmeasurable.
 * Now every install mints ONE EVM key on first run and keeps it in
 * ~/.agentpay/mcp-wallet.json (dir 0700, file 0600), so free and paid calls
 * share a stable `agent_address` across restarts.
 *
 * SPENDING IS A SEPARATE DECISION. Holding a key does not enable paid mode:
 * paid tools settle in-place only when AGENTPAY_ENABLE_PAID=1 (the persisted
 * wallet, once funded) or AGENTPAY_BASE_KEY is set (bring-your-own key —
 * explicit intent, preserves the 2.4.x behaviour). Without either, the wallet
 * is an identity and a funding target, nothing more.
 *
 * Failure honesty: a wallet file we cannot read or parse is NEVER overwritten
 * (it may guard funds) — the process falls back to an ephemeral in-memory key
 * and says so on stderr. An unwritable path gets the same fallback.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { generatePrivateKey, privateKeyToAddress } from './eip3009.js';

const WALLET_VERSION = 1;

export function defaultWalletPath(env = process.env, homedir = os.homedir) {
  const override = (env.AGENTPAY_WALLET_PATH || '').trim();
  if (override) return override;
  return path.join(homedir(), '.agentpay', 'mcp-wallet.json');
}

/**
 * Load the persisted wallet, minting one on first run.
 *
 * Returns { key, address, source, path, error? } where source is one of:
 *   'env'       — AGENTPAY_BASE_KEY (nothing touches disk)
 *   'file'      — loaded from an existing wallet file
 *   'minted'    — generated this run and persisted
 *   'ephemeral' — in-memory only (unreadable/corrupt/unwritable file); the
 *                 address changes next restart, `error` says why
 */
export function loadOrCreateWallet(env = process.env, log = () => {}, homedir = os.homedir) {
  const byoKey = (env.AGENTPAY_BASE_KEY || '').trim();
  if (byoKey) {
    try {
      return { key: byoKey, address: privateKeyToAddress(byoKey), source: 'env', path: null };
    } catch (err) {
      log(`AgentPay MCP: invalid AGENTPAY_BASE_KEY (${err.message}) — using the persisted wallet instead`);
    }
  }

  const walletPath = defaultWalletPath(env, homedir);

  if (fs.existsSync(walletPath)) {
    try {
      const data = JSON.parse(fs.readFileSync(walletPath, 'utf8'));
      const address = privateKeyToAddress(data.key);           // validates the key
      return { key: data.key, address, source: 'file', path: walletPath };
    } catch (err) {
      // NEVER overwrite a file we can't parse — it may guard funds.
      log(`AgentPay MCP: wallet file ${walletPath} is unreadable or corrupt (${err.message}). ` +
          'NOT overwriting it — running with an ephemeral in-memory wallet. ' +
          'Move the file aside to mint a fresh one.');
      return { ...ephemeral(), error: 'corrupt' };
    }
  }

  const key = generatePrivateKey();
  const address = privateKeyToAddress(key);
  try {
    fs.mkdirSync(path.dirname(walletPath), { recursive: true, mode: 0o700 });
    fs.writeFileSync(
      walletPath,
      JSON.stringify({ address, key, created_at: new Date().toISOString(),
                       version: WALLET_VERSION }, null, 2) + '\n',
      { mode: 0o600 },
    );
    return { key, address, source: 'minted', path: walletPath };
  } catch (err) {
    log(`AgentPay MCP: cannot write wallet file ${walletPath} (${err.message}) — ` +
        'running with an ephemeral in-memory wallet (identity resets on restart). ' +
        'Set AGENTPAY_WALLET_PATH to a writable location to persist it.');
    return { key, address, source: 'ephemeral', path: null, error: 'unwritable' };
  }
}

function ephemeral() {
  const key = generatePrivateKey();
  return { key, address: privateKeyToAddress(key), source: 'ephemeral', path: null };
}

/**
 * Is paid mode on? BYO key = yes (explicit intent, 2.4.x behaviour).
 * Otherwise only with AGENTPAY_ENABLE_PAID (1/true/yes). A persisted or
 * ephemeral wallet alone NEVER spends.
 */
export function paidModeEnabled(env = process.env, walletSource = 'file') {
  if (walletSource === 'env') return true;
  const flag = (env.AGENTPAY_ENABLE_PAID || '').trim().toLowerCase();
  return flag === '1' || flag === 'true' || flag === 'yes';
}
