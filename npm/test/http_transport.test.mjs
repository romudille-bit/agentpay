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
  { name: 'token_price', description: 'x', price_usdc: '0.000', parameters: { type: 'object', properties: {} } },
  { name: 'verified_route', description: 'x', price_usdc: '0.01', parameters: { type: 'object', properties: {} } },
] };

// What the gateway answers once a payment has been presented and not delivered on.
const AFTER_PAYMENT = {
  '0xbad': [402, { error: 'settlement failed', reason: 'invalid_signature' }],
  '0xuncertain': [503, { error: 'Stacks settlement uncertain', payment_status: 'uncertain',
                         error_reason: 'confirmation_timeout', payment_id: 'pid-1', txid: '0xstx1' }],
  '0xtoolfail': [500, { error: 'Tool execution failed', payment_id: '0xtx2',
                        payment_status: 'refund_disabled', error_reason: 'tool_exec_failed: boom' }],
  '0xedge': [502, '<html>bad gateway</html>'],
};

function fakeGateway() {
  const seen = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    seen.push({ url: req.url, headers: req.headers, body });
    let status = 200, json = {};
    if (req.url === '/tools') json = toolsList;
    else if (req.url.startsWith('/tools/') && req.headers['payment-signature']) {
      const paid = JSON.parse(Buffer.from(req.headers['payment-signature'], 'base64').toString());
      if (AFTER_PAYMENT[paid.payload?.signature]) [status, json] = AFTER_PAYMENT[paid.payload.signature];
      else json = { tool: 'pre_trade_check', result: { verdict: 'ok' },
                    payment: { amount_usdc: '0.01', tx_hash: '0xtx1', network: 'base-mainnet' } };
    } else if (req.url.includes('token_price')) { status = 429; json = { error: 'Rate limit exceeded' };
    } else if (req.url.startsWith('/tools/') && !req.headers['x-payment']) {
      const free = req.url.includes('fear_greed');
      status = 402;
      json = { x402Version: 2, error: 'Payment required', payment_id: 'pid-1',
               amount_usdc: free ? '0.000' : '0.01', pay_to: '0x' + 'cc'.repeat(20),
               resource: { url: 'http://x/tools/pre_trade_check/call', mimeType: 'application/json' },
               accepts: free ? [] : [{ scheme: 'exact', network: 'eip155:8453', amount: '10000',
                 asset: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', payTo: '0x' + 'cc'.repeat(20),
                 maxTimeoutSeconds: 300, extra: { name: 'USD Coin', version: '2' } }] };
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
      AGENTPAY_BAZAAR_URL: `${gatewayUrl}/discovery/search`,
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
  return { base, stop, stderr: () => err, walletPath: path.join(dir, 'w.json') };
}

async function rpc(base, id, method, params = {}) {
  return rpcRaw(base, { jsonrpc: '2.0', id, method, params });
}

async function rpcRaw(base, msg) {
  const res = await fetch(`${base}/mcp`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
    body: JSON.stringify(msg),
  });
  const text = await res.text();
  const line = text.split('\n').find((l) => l.startsWith('data: '));
  return { status: res.status, msg: JSON.parse(line ? line.slice(6) : text) };
}

test('http mode: initialize, list, free call, x402-over-MCP paid flow, server never signs', async () => {
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
    // Free tools are read-only; the priced one is not — a wallet-carrying
    // client pays the in-band 402, so the harness must prompt before calling it.
    const byName = Object.fromEntries(list.msg.result.tools.map((t) => [t.name, t.annotations.readOnlyHint]));
    assert.equal(byName.fear_greed_index, true);
    assert.equal(byName.estimate_plan, true);
    assert.equal(byName.pre_trade_check, false);
    assert.equal(list.msg.result.tools[0].name, 'pre_trade_check', 'priced tools are listed first on the remote');
    // verified_route is the gateway's paid tool; the free preview is listed beside it.
    assert.equal(byName.verified_route, false);
    assert.equal(byName.verified_route_preview, true);
    assert.match(list.msg.result.tools.find((t) => t.name === 'verified_route_preview').description, /call verified_route \(\$0\.01/);

    const free = await rpc(srv.base, 3, 'tools/call', { name: 'fear_greed_index', arguments: {} });
    assert.equal(free.status, 200);
    assert.match(free.msg.result.content[0].text, /42/);
    const paidRetry = gw.seen.find((r) => r.headers['x-payment']);
    assert.ok(paidRetry, 'free retry carried X-Payment');
    assert.match(paidRetry.headers['x-payment'], /^tx_hash=free:pid-1,from=0xab/i);
    assert.equal(paidRetry.headers['x-agent-address'], IDENTITY);

    // x402 over MCP: unpaid call → result.isError + PaymentRequired (dual format)
    const paid = await rpc(srv.base, 4, 'tools/call', { name: 'pre_trade_check', arguments: {} });
    assert.equal(paid.status, 200);
    const pr = paid.msg.result;
    assert.equal(pr.isError, true);
    assert.equal(pr.structuredContent.x402Version, 2);
    assert.equal(pr.structuredContent.accepts[0].network, 'eip155:8453');
    assert.deepEqual(JSON.parse(pr.content[0].text), pr.structuredContent);
    assert.match(pr.structuredContent.error, /wallet-carrying MCP client/);

    // An unpaid verified_route answers PaymentRequired; the preview never charges,
    // even when a payment is attached.
    const full = await rpc(srv.base, 41, 'tools/call', { name: 'verified_route', arguments: { need: 'dex liquidity' } });
    assert.equal(full.msg.result.isError, true);
    assert.equal(full.msg.result.structuredContent.accepts[0].network, 'eip155:8453');
    assert.ok(gw.seen.some((r) => r.url === '/tools/verified_route/call'), 'it calls the gateway tool');
    const preview = await rpcRaw(srv.base, { jsonrpc: '2.0', id: 42, method: 'tools/call',
      params: { name: 'verified_route_preview', arguments: { need: 'dex liquidity' },
                _meta: { 'x402/payment': { x402Version: 2, payload: { signature: '0xsig' } } } } });
    assert.equal(preview.msg.result.structuredContent, undefined);
    assert.match(preview.msg.result.content[0].text, /PREVIEW/);
    assert.ok(!gw.seen.some((r) => r.headers['payment-signature']), 'the server signed nothing');

    // The client pays in-band: _meta x402/payment → PAYMENT-SIGNATURE, receipt in _meta
    const payment = { x402Version: 2, accepted: pr.structuredContent.accepts[0],
      payload: { signature: '0xsig', authorization: { from: '0x' + 'ab'.repeat(20), to: '0x' + 'cc'.repeat(20),
                 value: '10000', validAfter: '0', validBefore: '9999999999', nonce: '0x' + '11'.repeat(32) } } };
    const settled = await rpcRaw(srv.base, { jsonrpc: '2.0', id: 5, method: 'tools/call',
      params: { name: 'pre_trade_check', arguments: { symbol: 'ETH' }, _meta: { 'x402/payment': payment } } });
    assert.equal(settled.msg.result.isError, undefined);
    assert.match(settled.msg.result.content[0].text, /"verdict": "ok"/);
    const rcpt = settled.msg.result._meta['x402/payment-response'];
    assert.equal(rcpt.success, true);
    assert.equal(rcpt.transaction, '0xtx1');
    assert.equal(rcpt.payer, '0x' + 'ab'.repeat(20));
    const sig = gw.seen.find((r) => r.headers['payment-signature']);
    assert.deepEqual(JSON.parse(Buffer.from(sig.headers['payment-signature'], 'base64').toString()), payment);
    assert.equal(sig.headers['x-agent-address'], '0x' + 'ab'.repeat(20), 'payer is the identity on a paid call');

    // A rejected payment comes back as PaymentRequired again, never as a settle
    const bad = await rpcRaw(srv.base, { jsonrpc: '2.0', id: 6, method: 'tools/call',
      params: { name: 'pre_trade_check', arguments: {}, _meta: { 'x402/payment': { ...payment, payload: { ...payment.payload, signature: '0xbad' } } } } });
    assert.equal(bad.msg.result.isError, true);
    assert.equal(bad.msg.result.structuredContent.x402Version, 2);
    // The gateway's rejection body has no accepts; the server re-fetched the
    // challenge so the client can retry, and put the reason first.
    assert.match(bad.msg.result.structuredContent.error, /^payment rejected \(invalid_signature\) — 'pre_trade_check' costs \$0\.01/);
    assert.equal(bad.msg.result.structuredContent.accepts[0].network, 'eip155:8453');

    assert.equal((await fetch(`${srv.base}/mcp`)).status, 405);
    assert.equal((await fetch(`${srv.base}/mcp`, { method: 'DELETE' })).status, 405);
    assert.match(srv.stderr(), /remote \(http\) mode/);
    assert.ok(!fs.existsSync(srv.walletPath), 'remote mode never writes a wallet file');
    assert.doesNotMatch(srv.stderr(), /AgentPay MCP: wallet /);
  } finally {
    srv.stop();
    gw.server.close();
  }
});

test('http mode: a paid call that fails after payment keeps the receipt; 429 reads as a rate limit', async () => {
  const gw = await fakeGateway();
  const srv = await startHttp(gw.url);
  const pay = (id, signature) => rpcRaw(srv.base, { jsonrpc: '2.0', id, method: 'tools/call',
    params: { name: 'pre_trade_check', arguments: {}, _meta: { 'x402/payment': {
      x402Version: 2, accepted: { network: 'stacks:1' }, payload: { signature } } } } });
  try {
    // Broadcast but unconfirmed: the client is told to re-present, not to pay again.
    const unc = (await pay(1, '0xuncertain')).msg.result;
    assert.equal(unc.isError, true);
    assert.equal(unc.structuredContent, undefined, 'not a PaymentRequired: nothing to pay again');
    assert.match(unc.content[0].text, /Do NOT sign a new payment.*0xstx1/);
    assert.deepEqual(unc._meta['x402/payment-response'],
      { success: false, errorReason: 'uncertain', transaction: '0xstx1', network: 'stacks:1', payer: null });

    // Settled, then the tool failed: the transaction id survives.
    const failed = (await pay(2, '0xtoolfail')).msg.result;
    assert.match(failed.content[0].text, /payment settled \(transaction 0xtx2\) but the tool failed/);
    assert.doesNotMatch(failed.content[0].text, /boom/, 'gateway internals stay out of the reply');
    assert.equal(failed._meta['x402/payment-response'].transaction, '0xtx2');
    assert.equal(failed._meta['x402/payment-response'].success, true);

    // An edge error page after the payment was sent: outcome unknown, said so.
    const edge = (await pay(3, '0xedge')).msg.result;
    assert.match(edge.content[0].text, /answered 502 after the payment was sent/);
    assert.equal(edge._meta['x402/payment-response'].errorReason, 'unknown');

    const limited = await rpc(srv.base, 4, 'tools/call', { name: 'token_price', arguments: {} });
    assert.match(limited.msg.result.content[0].text, /rate limited at the gateway/);
  } finally {
    srv.stop();
    gw.server.close();
  }
});
