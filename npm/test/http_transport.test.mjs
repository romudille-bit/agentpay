/**
 * http_transport.test.mjs — the remote (Streamable HTTP) transport, AGE-127.
 *
 * The shared server must stay keyless whatever the env says: a key and
 * AGENTPAY_ENABLE_PAID=1 are deliberately passed here and must not turn paid
 * mode on. Everything else is the stdio behaviour reached over POST /mcp.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const BIN = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'bin', 'agentpay-mcp.js');
const KEY = '0x' + '11'.repeat(32);
const IDENTITY = '0x' + 'ab'.repeat(20);

const toolsList = { tools: [
  { name: 'fear_greed_index', description: 'x', price_usdc: '0.000', parameters: { type: 'object', properties: {} } },
  { name: 'pre_trade_check', description: 'x', price_usdc: '0.01', parameters: { type: 'object', properties: {} } },
] };

function fakeGateway() {
  const seen = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    seen.push({ url: req.url, headers: req.headers, body });
    let status = 200, json = {};
    if (req.url === '/tools') json = toolsList;
    else if (req.url.startsWith('/tools/') && !req.headers['x-payment']) {
      const free = req.url.includes('fear_greed');
      status = 402;
      json = { payment_id: 'pid-1', amount_usdc: free ? '0.000' : '0.01', pay_to: '0x' + 'cc'.repeat(20) };
    } else json = { result: { value: 42 } };
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(json));
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({ server, seen, url: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

function freePort() {
  return new Promise((resolve) => {
    const s = http.createServer();
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

async function startHttp(gatewayUrl) {
  const port = await freePort();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentpay-mcp-http-'));
  const child = spawn(process.execPath, [BIN, '--http'], {
    env: {
      ...process.env,
      AGENTPAY_GATEWAY_URL: gatewayUrl,
      AGENTPAY_BASE_KEY: KEY,            // must be ignored in http mode
      AGENTPAY_ENABLE_PAID: '1',         // must be ignored in http mode
      AGENTPAY_AGENT_ADDRESS: IDENTITY,
      AGENTPAY_WALLET_PATH: path.join(dir, 'w.json'),
      PORT: String(port), HOST: '127.0.0.1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let err = '';
  child.stderr.on('data', (d) => { err += d; });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  const stop = () => { child.kill(); fs.rmSync(dir, { recursive: true, force: true }); };
  return { base, stop, stderr: () => err };
}

async function rpc(base, id, method, params = {}) {
  const res = await fetch(`${base}/mcp`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
    body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
  });
  const text = await res.text();
  const line = text.split('\n').find((l) => l.startsWith('data: '));
  return { status: res.status, msg: JSON.parse(line ? line.slice(6) : text) };
}

test('http mode: initialize, list, free call round-trip, and paid stays refused', async () => {
  const gw = await fakeGateway();
  const srv = await startHttp(gw.url);
  try {
    const health = await (await fetch(`${srv.base}/health`)).json();
    assert.equal(health.paid, false);
    assert.equal(health.transport, 'http');

    const init = await rpc(srv.base, 1, 'initialize', {
      protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '0' } });
    assert.equal(init.status, 200);
    assert.equal(init.msg.result.serverInfo.name, 'agentpay');

    const list = await rpc(srv.base, 2, 'tools/list');
    const names = list.msg.result.tools.map((t) => t.name);
    assert.ok(names.includes('fear_greed_index') && names.includes('estimate_plan'));
    // Keyless: every tool is advertised read-only, including the priced one.
    assert.ok(list.msg.result.tools.every((t) => t.annotations.readOnlyHint === true));

    const free = await rpc(srv.base, 3, 'tools/call', { name: 'fear_greed_index', arguments: {} });
    assert.equal(free.status, 200);
    assert.match(free.msg.result.content[0].text, /42/);
    const paidRetry = gw.seen.find((r) => r.headers['x-payment']);
    assert.ok(paidRetry, 'free retry carried X-Payment');
    assert.match(paidRetry.headers['x-payment'], /^tx_hash=free:pid-1,from=0xab/i);
    assert.equal(paidRetry.headers['x-agent-address'], IDENTITY);

    const paid = await rpc(srv.base, 4, 'tools/call', { name: 'pre_trade_check', arguments: {} });
    assert.ok(paid.msg.error, 'paid tool is an error, not a settle');
    assert.match(paid.msg.error.message, /shared remote server/);
    assert.ok(!gw.seen.some((r) => r.headers['payment-signature']), 'nothing was ever signed');

    assert.equal((await fetch(`${srv.base}/mcp`)).status, 405);
    assert.equal((await fetch(`${srv.base}/mcp`, { method: 'DELETE' })).status, 405);
    assert.match(srv.stderr(), /remote \(http\) mode/);
  } finally {
    srv.stop();
    gw.server.close();
  }
});
