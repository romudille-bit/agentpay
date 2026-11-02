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
                                      calls = 1, bazaarUrl, args = { symbol: 'BTC' },
                                      env = {}, sequence = null } = {}) {
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
      ...env,
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
  const seq = sequence ?? Array.from({ length: calls }, () => ({ name, arguments: args }));
  calls = seq.length;
  for (let i = 0; i < calls; i++) {
    send({ jsonrpc: '2.0', id: 10 + i, method: 'tools/call', params: seq[i] });
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
  assert.deepEqual(names, ['verified_route', 'route', 'estimate_plan', 'session_cap']);
});

test('list_tools refuses a payload with no tools array rather than caching it', async () => {
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: { message: 'nothing here' } };
    return { status: 500, json: {} };
  });
  const reply = await listOnce(g.url);
  g.server.close();

  const names = (reply?.result?.tools || []).map((t) => t.name);
  assert.deepEqual(names, ['verified_route', 'route', 'estimate_plan', 'session_cap']);
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

test('verified_route_preview is a remote-only name: stdio keeps one verified_route', async () => {
  const g = await fakeGateway((req) => (req.url === '/tools'
    ? { json: { tools: [...toolsList.tools, { name: 'verified_route', description: 'x', price_usdc: '0.01' }] } }
    : { json: {} }));
  const { replies } = await callOnce(g.url, { name: 'verified_route_preview', args: { need: 'x' } });
  g.server.close();
  assert.equal(replies[0].error.code, -32601);
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

// ── Stacks (sBTC) settlement + the cap/receipt rule ──────────────────────────

// Throwaway fixture key (NEVER fund): tests/fixtures/stacks_tx_fixtures.json keys[0].
const STACKS_KEY = '000000000000000000000000000000000000000000000000000000000000000101';
const STACKS_ADDR = 'ST1THWXQ8368SDN2MJGE4BMDKMCHZ2GSVTSQDA7QF';
const PAYEE = 'ST2ZD731ANQZT6J4K3F5N8A40ZXWXC1XFXH4HF6PF';
const RELAYED = 'ab'.repeat(32);

function stacksChallenge({ sponsored = true } = {}) {
  return {
    payment_id: 'pid-stx-1', amount_usdc: '0.01', pay_to: PAYEE,
    payment_options: {
      stacks: { scheme: 'exact', network: 'stacks:2147483648', amount_sats: 10, amount_usdc: '0.01',
                btc_usd_rate: '100000', pay_to: PAYEE, fee_microstx: 3000, asset: 'sbtc',
                ...(sponsored ? { sponsorship: { available: true, relay: 'velumx', origin_fee_microstx: 0 } } : {}) },
    },
  };
}

const stacksEnv = (gwUrl, extra = {}) => ({ AGENTPAY_STACKS_KEY: STACKS_KEY, AGENTPAY_STACKS_NETWORK: 'testnet',
                                            AGENTPAY_STACKS_API: gwUrl, ...extra });

/** Gateway routes shared by the Stacks tests: nonce read + 402 + settle. */
function stacksRoutes(onSettle) {
  return (req) => {
    if (req.url === '/tools') return { json: toolsList };
    if (req.url.startsWith('/v2/accounts/')) return { json: { nonce: 4, balance: '0x0' } };
    if (req.headers['payment-signature']) return onSettle(req);
    return { status: 402, json: stacksChallenge() };
  };
}

const decodeHeader = (req) => JSON.parse(Buffer.from(req.headers['payment-signature'], 'base64').toString());

test('a Stacks key settles in sBTC: sponsored when offered, receipt returned with the result', async () => {
  const g = await fakeGateway(stacksRoutes(() => ({
    json: { tool: 'pre_trade_check', result: { verdict: 'ok' },
            payment: { tx_hash: RELAYED, network: 'stacks-testnet', amount_usdc: '0.01' } },
  })));
  const { replies, stderr } = await callOnce(g.url, { env: stacksEnv(g.url) });
  g.server.close();

  const out = JSON.parse(textOf(replies[0]));
  assert.equal(out.verdict, 'ok');
  assert.equal(out.receipt.tx_hash, RELAYED);
  assert.equal(out.receipt.network, 'stacks-testnet');
  assert.equal(out.receipt.sponsored, true);
  assert.equal(out.receipt.amount_sats, 10);
  assert.equal(out.receipt.payer, STACKS_ADDR);
  assert.equal(out.receipt.session_spent_usd, '0.01');
  assert.equal(out.receipt.session_cap_usd, '0.1');
  assert.match(out.receipt.explorer_url, /explorer\.hiro\.so\/txid\/0xab.*chain=testnet/);

  const settle = g.seen.find((r) => r.headers['payment-signature']);
  assert.equal(settle.headers['x-agent-address'], STACKS_ADDR);
  const payload = decodeHeader(settle);
  assert.equal(payload.network, 'stacks:2147483648');
  assert.equal(payload.payment_id, 'pid-stx-1');
  assert.equal(payload.payload.sponsored, true);
  const tx = Buffer.from(payload.payload.signedTransaction, 'hex');
  assert.equal(tx[5], 0x05);                                   // sponsored auth
  assert.equal(tx.readBigUInt64BE(35), 0n);                     // origin fee 0
  assert.equal(tx.readBigUInt64BE(27), 4n);                     // nonce from Hiro
  assert.match(stderr, /settling 'pre_trade_check' on Stacks \(10 sats sBTC, sponsored\)/);
});

test('AGENTPAY_STACKS_SPONSORED=0 pays its own fee even when sponsorship is offered', async () => {
  const g = await fakeGateway(stacksRoutes(() => ({ json: { result: { ok: true }, payment: { tx_hash: 'cd'.repeat(32) } } })));
  const { replies } = await callOnce(g.url, { env: stacksEnv(g.url, { AGENTPAY_STACKS_SPONSORED: '0' }) });
  g.server.close();
  const out = JSON.parse(textOf(replies[0]));
  assert.equal(out.receipt.sponsored, false);
  const tx = Buffer.from(decodeHeader(g.seen.find((r) => r.headers['payment-signature'])).payload.signedTransaction, 'hex');
  assert.equal(tx[5], 0x04);
  assert.equal(tx.readBigUInt64BE(35), 3000n);
});

test('a Base receipt is returned with the result too', async () => {
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: toolsList };
    if (req.headers['payment-signature']) {
      return { json: { result: { verdict: 'ok' }, payment: { tx_hash: '0x' + 'ee'.repeat(32), network: 'base-mainnet' } } };
    }
    return { status: 402, json: challenge() };
  });
  const { replies } = await callOnce(g.url);
  g.server.close();
  const out = JSON.parse(textOf(replies[0]));
  assert.equal(out.verdict, 'ok');
  assert.equal(out.receipt.network, 'base-mainnet');
  assert.equal(out.receipt.session_spent_usd, '0.01');
  assert.match(out.receipt.explorer_url, /basescan\.org\/tx\/0xee/);
});

test('a rejected Stacks settle that never broadcast gives the cap back; the next call re-signs', async () => {
  let settles = 0;
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: toolsList };
    if (req.url.startsWith('/v2/accounts/')) return { json: { nonce: 4, balance: '0x0' } };
    if (req.url.startsWith('/extended/v1/address/')) return { json: { results: [] } };   // nonce not on chain
    if (req.headers['payment-signature']) {
      settles++;
      return settles === 1
        ? { status: 402, json: { payment_status: 'rejected', error_reason: 'sponsor_rejected: cap exceeded' } }
        : { json: { result: { ok: true }, payment: { tx_hash: RELAYED } } };
    }
    return { status: 402, json: stacksChallenge() };
  });
  const { replies } = await callOnce(g.url, { env: stacksEnv(g.url), calls: 2 });
  g.server.close();
  assert.match(textOf(replies[0]), /rejected .*nothing was broadcast, the cap is unchanged/);
  const out = JSON.parse(textOf(replies[1]));
  assert.equal(out.receipt.session_spent_usd, '0.01');   // only the second settle counted
  assert.equal(settles, 2);
});

test('an uncertain Stacks settle is redeemed on the next call instead of paying twice', async () => {
  let settles = 0;
  const g = await fakeGateway(stacksRoutes(() => {
    settles++;
    return settles === 1
      ? { status: 503, json: { payment_status: 'uncertain', error_reason: 'relay_accepted_pending_confirmation', txid: RELAYED } }
      : { json: { result: { ok: true }, payment: { tx_hash: RELAYED } } };
  }));
  const { replies } = await callOnce(g.url, { env: stacksEnv(g.url), calls: 2 });
  g.server.close();
  assert.match(textOf(replies[0]), /not confirmed .*do not sign a new one/);
  const out = JSON.parse(textOf(replies[1]));
  assert.equal(out.ok, true);
  assert.equal(out.receipt.tx_hash, RELAYED);
  assert.equal(out.receipt.session_spent_usd, '0.01');   // booked once
  const headers = g.seen.filter((r) => r.headers['payment-signature']).map(decodeHeader);
  assert.equal(headers.length, 2);
  assert.equal(headers[0].payload.txid, headers[1].payload.txid);   // same signed bytes
});

test('session_cap reports and lowers the cap; raising above the ceiling is refused', async () => {
  const g = await fakeGateway(stacksRoutes(() => ({ json: { result: { ok: true }, payment: { tx_hash: RELAYED } } })));
  const { replies } = await callOnce(g.url, {
    env: stacksEnv(g.url),
    sequence: [
      { name: 'session_cap', arguments: {} },
      { name: 'session_cap', arguments: { max_usd: 0.005 } },
      { name: 'pre_trade_check', arguments: { symbol: 'BTC' } },
      { name: 'session_cap', arguments: { max_usd: 5 } },
    ],
  });
  g.server.close();
  const first = JSON.parse(textOf(replies[0]));
  assert.equal(first.cap_usd, '0.1');
  assert.equal(first.wallets.stacks, STACKS_ADDR);
  assert.equal(first.stacks_sponsored, 'auto');
  assert.equal(JSON.parse(textOf(replies[1])).cap_usd, '0.005');
  assert.match(textOf(replies[2]), /Budget cap/);
  assert.match(textOf(replies[3]), /above the configured ceiling/);
  assert.equal(g.seen.filter((r) => r.headers['payment-signature']).length, 0);
});

test('with a Stacks key and a Base-only 402, the call settles on Base', async () => {
  const g = await fakeGateway((req) => {
    if (req.url === '/tools') return { json: toolsList };
    if (req.headers['payment-signature']) return { json: { result: { ok: true }, payment: { tx_hash: '0x' + 'ee'.repeat(32), network: 'base-mainnet' } } };
    return { status: 402, json: challenge() };
  });
  const { replies, stderr } = await callOnce(g.url, { env: stacksEnv(g.url) });
  g.server.close();
  assert.equal(JSON.parse(textOf(replies[0])).receipt.network, 'base-mainnet');
  assert.match(stderr, /offers no Stacks option — settling on Base/);
});
