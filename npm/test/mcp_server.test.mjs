/**
 * mcp_server.test.mjs — the MCP server against a fake gateway, over real stdio.
 *
 * settlePaid and the request handlers had no coverage, which is where the
 * spend-control bugs lived: the cap was checked against one field and the
 * signature built from another, and a spend was booked only when the settle
 * reply arrived. Both are about a 402 the server does not control —
 * AGENTPAY_GATEWAY_URL is an env var — so the tests drive the real binary with
 * crafted 402s and watch what it signs, sends and counts.
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
const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';

/** A gateway that answers from a queue of handlers and records every request. */
function fakeGateway(routes) {
  const seen = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    seen.push({ url: req.url, method: req.method, headers: req.headers, body });
    const handler = routes(req, seen.length);
    const { status = 200, json = {} } = handler || {};
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(json));
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({ server, seen, url: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

const toolsList = { tools: [{ name: 'pre_trade_check', description: 'x', price_usdc: '0.01',
                              parameters: { type: 'object', properties: {} } }] };

function challenge({ usdc = '0.01', atomic = '10000' } = {}) {
  return {
    payment_id: 'pid-1',
    amount_usdc: usdc,
    pay_to: '0x' + 'cc'.repeat(20),
    payment_options: {
      base: {
        amount_atomic: atomic,
        asset: USDC,
        network: 'eip155:8453',
        pay_to: '0x' + 'cc'.repeat(20),
        resource: 'https://example.test/tools/pre_trade_check/call',
      },
    },
  };
}

/** Start the server, run one tools/call, return its result plus stderr. */
async function callOnce(gatewayUrl, { name = 'pre_trade_check', maxSpend = '0.10',
                                      calls = 1, bazaarUrl, args = { symbol: 'BTC' } } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentpay-mcp-'));
  const child = spawn(process.execPath, [BIN], {
    env: {
      ...process.env,
      AGENTPAY_GATEWAY_URL: gatewayUrl,
      AGENTPAY_BASE_KEY: KEY,
      AGENTPAY_ENABLE_PAID: '1',
      AGENTPAY_MAX_SPEND: maxSpend,
      AGENTPAY_WALLET_PATH: path.join(dir, 'w.json'),
      ...(bazaarUrl ? { AGENTPAY_BAZAAR_URL: bazaarUrl } : {}),
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  let out = '', err = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { err += d; });

  const send = (msg) => child.stdin.write(JSON.stringify(msg) + '\n');
  send({ jsonrpc: '2.0', id: 1, method: 'initialize',
         params: { protocolVersion: '2024-11-05', capabilities: {},
                   clientInfo: { name: 'test', version: '0' } } });
  send({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} });
  for (let i = 0; i < calls; i++) {
    send({ jsonrpc: '2.0', id: 10 + i, method: 'tools/call',
           params: { name, arguments: args } });
  }

  const replies = await new Promise((resolve) => {
    const deadline = setTimeout(() => resolve(parse(out)), 6000);
    const tick = setInterval(() => {
      const got = parse(out);
      if (got.filter((m) => m.id >= 10).length >= calls) {
        clearTimeout(deadline); clearInterval(tick); resolve(got);
      }
    }, 50);
  });
  child.kill();
  fs.rmSync(dir, { recursive: true, force: true });
  return {
    replies: replies.filter((m) => m.id >= 10).sort((a, b) => a.id - b.id),
    stderr: err,
  };
}

/** Ask for the tool list and nothing else. */
async function listOnce(gatewayUrl) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentpay-mcp-'));
  const child = spawn(process.execPath, [BIN], {
    env: { ...process.env, AGENTPAY_GATEWAY_URL: gatewayUrl,
           AGENTPAY_WALLET_PATH: path.join(dir, 'w.json') },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let out = '';
  child.stdout.on('data', (d) => { out += d; });
  const send = (m) => child.stdin.write(JSON.stringify(m) + '\n');
  send({ jsonrpc: '2.0', id: 1, method: 'initialize',
         params: { protocolVersion: '2024-11-05', capabilities: {},
                   clientInfo: { name: 'test', version: '0' } } });
  send({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} });
  send({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} });

  const reply = await new Promise((resolve) => {
    const deadline = setTimeout(() => resolve(null), 6000);
    const tick = setInterval(() => {
      const got = parse(out).find((m) => m.id === 2);
      if (got) { clearTimeout(deadline); clearInterval(tick); resolve(got); }
    }, 50);
  });
  child.kill();
  fs.rmSync(dir, { recursive: true, force: true });
  return reply;
}

function parse(buf) {
  return buf.split('\n').filter(Boolean).flatMap((line) => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
}

const textOf = (reply) =>
  (reply?.result?.content?.[0]?.text ?? reply?.error?.message ?? '');

test('a Base option that would sign for more than the quoted price is refused before signing', async () => {
  // $0.01 quoted, $10 in the option that actually gets signed.
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: toolsList };
    return { status: 402, json: challenge({ usdc: '0.01', atomic: '10000000' }) };
  });
  const { replies } = await callOnce(g.url);
  g.server.close();

  assert.match(textOf(replies[0]), /refusing to sign/i);
  // One POST only: the 402. Nothing was signed, so nothing was transmitted.
  const posts = g.seen.filter((r) => r.method === 'POST');
  assert.equal(posts.length, 1);
  assert.ok(!posts.some((p) => p.headers['payment-signature']));
});

test('a quote with no readable amount is refused rather than disabling the cap', async () => {
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: toolsList };
    const c = challenge();
    delete c.amount_usdc;
    delete c.payment_options.base.amount_atomic;
    delete c.payment_options.base.amount;
    return { status: 402, json: c };
  });
  const { replies } = await callOnce(g.url);
  g.server.close();

  assert.match(textOf(replies[0]), /unreadable|refusing to sign/i);
  assert.equal(g.seen.filter((r) => r.method === 'POST').length, 1);
});

test('a spend is counted at transmission, so a failed settle still charges the cap', async () => {
  // Cap $0.015 and a $0.01 tool: the first settle is transmitted then 500s, so
  // the second call must be refused by the cap — the authorization from the
  // first is settleable whatever that 500 said.
  const g = await fakeGateway((req, n) => {
    if (req.url === '/tools') return { json: toolsList };
    if (req.headers && req.headers['payment-signature']) {
      return { status: 500, json: { error: 'boom' } };
    }
    return { status: 402, json: challenge() };
  });
  const { replies } = await callOnce(g.url, { maxSpend: '0.015', calls: 2 });
  g.server.close();

  const texts = replies.map(textOf);
  assert.ok(texts.some((t) => /stays counted against/i.test(t)),
            `no transmitted-spend notice in ${JSON.stringify(texts)}`);
  assert.ok(texts.some((t) => /Budget cap/i.test(t)),
            `the cap never refused the second call: ${JSON.stringify(texts)}`);
  // The second call never reached a signature.
  const signed = g.seen.filter((r) => r.headers['payment-signature']);
  assert.equal(signed.length, 1);
});

test('list_tools still serves the keyless tools when the gateway is unreachable', async () => {
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { status: 503, json: { error: 'down' } };
    return { status: 500, json: {} };
  });
  const reply = await listOnce(g.url);
  g.server.close();

  const names = (reply?.result?.tools || []).map((t) => t.name);
  assert.deepEqual(names, ['verified_route', 'route', 'estimate_plan']);
});

test('list_tools refuses a payload with no tools array rather than caching it', async () => {
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: { message: 'nothing here' } };
    return { status: 500, json: {} };
  });
  const reply = await listOnce(g.url);
  g.server.close();

  const names = (reply?.result?.tools || []).map((t) => t.name);
  assert.deepEqual(names, ['verified_route', 'route', 'estimate_plan']);
});

test('an unknown tool name is a protocol error, not a gateway call', async () => {
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: toolsList };
    return { status: 402, json: challenge() };
  });
  const { replies } = await callOnce(g.url, { name: '../admin/thing' });
  g.server.close();

  assert.match(textOf(replies[0]), /Unknown tool/i);
  assert.equal(g.seen.filter((r) => r.method === 'POST').length, 0);
});

test('a listing priced in decimal USD is not reported as free', async () => {
  // Bazaar listings carry either atomic units or an already-decimal figure;
  // reading "0.01" as atomic priced a real paid tool at $0.00 and then ranked it
  // first on the price tiebreak.
  const listing = {
    resources: [{
      resource: { url: 'https://seller.test/tool', serviceName: 'seller' },
      accepts: [{ amount: '0.01', asset: USDC, network: 'eip155:8453',
                  payTo: '0x' + 'dd'.repeat(20) }],
      quality: { l30DaysTotalCalls: '500', l30DaysUniquePayers: '5' },
    }],
  };
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: toolsList };
    if (req.url && req.url.startsWith('/discovery')) return { json: listing };
    return { json: listing };
  });
  const { replies } = await callOnce(g.url, {
    name: 'route', bazaarUrl: `${g.url}/discovery/search`,
    args: { need: 'market data' },
  });
  g.server.close();

  const text = textOf(replies[0]);
  // Not free, and priced as the decimal it advertised.
  assert.doesNotMatch(text, /"price_usd":\s*0\s*[,}]/);
  assert.match(text, /"price_usd":\s*0\.01/);
});
