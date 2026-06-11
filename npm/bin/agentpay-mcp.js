#!/usr/bin/env node
/**
 * agentpay-mcp.js — AgentPay MCP Server (Node-native, self-contained)
 *
 * Exposes AgentPay's 17 free tools as MCP tools via stdio transport,
 * plus a `route` tool for buyer-side x402 marketplace routing (MCP-2).
 * No Python, no repo checkout, no wallet — runs anywhere with Node ≥ 18.
 *
 * x402 free-flow (mirrors agentpay/_client.py):
 *   1. POST /tools/{name}/call → 402 with payment_id, amount_usdc = "0"
 *   2. Retry with X-Payment: tx_hash=free:<id>,from=<addr>,id=<id>
 *   3. Return result["result"] to the MCP caller
 *
 * route(need, budget) — MCP-2:
 *   Discover across Coinbase Bazaar, junk-filter, usage-rank, budget-gate,
 *   price-tiebreak → return ranked candidates + recommendation + ready-to-pay
 *   details. Advise-only, keyless. No payment happens here.
 *
 * Usage:
 *   npx @romudille/agentpay-mcp
 *
 * Env:
 *   AGENTPAY_GATEWAY_URL  — default https://agentpay.tools
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
  ErrorCode,
  McpError,
} from '@modelcontextprotocol/sdk/types.js';
import { randomUUID } from 'crypto';

// ── Config ────────────────────────────────────────────────────────────────────

const VERSION = '2.2.0';
const GATEWAY_URL = (process.env.AGENTPAY_GATEWAY_URL || 'https://agentpay.tools').replace(/\/$/, '');

// Ephemeral agent identity — free calls use it only for the `from=` field in
// X-Payment (gateway logs only). A UUID is simpler and equally valid here.
const AGENT_ADDRESS = `mcp-free-${randomUUID()}`;

const USER_AGENT = `agentpay-mcp/${VERSION} (+https://agentpay.tools)`;

// Silence all non-critical logging — any stray stdout corrupts the MCP stream.
// All diagnostic output goes to stderr.
const log = (...args) => process.stderr.write(args.join(' ') + '\n');

// ── Tool registry cache ───────────────────────────────────────────────────────

let _tools = null;

async function fetchTools() {
  const resp = await fetch(`${GATEWAY_URL}/tools`, {
    headers: { 'User-Agent': USER_AGENT },
    signal: AbortSignal.timeout(10_000),
  });
  if (!resp.ok) throw new Error(`GET /tools → ${resp.status}`);
  const data = await resp.json();
  return data.tools;
}

async function getTools() {
  if (!_tools) _tools = await fetchTools();
  return _tools;
}

// ── x402 free-flow ────────────────────────────────────────────────────────────

async function callTool(toolName, params) {
  const url = `${GATEWAY_URL}/tools/${toolName}/call`;
  const body = JSON.stringify({ parameters: params, agent_address: AGENT_ADDRESS });
  const baseHeaders = {
    'Content-Type': 'application/json',
    'User-Agent': USER_AGENT,
  };

  // Step 1: initial POST — expect 402
  const r1 = await fetch(url, {
    method: 'POST',
    headers: baseHeaders,
    body,
    signal: AbortSignal.timeout(45_000),
  });

  if (r1.status === 200) {
    // Shouldn't happen on first call, but handle it gracefully
    const data = await r1.json();
    return data.result ?? data;
  }

  if (r1.status !== 402) {
    const text = await r1.text();
    throw new Error(`Unexpected status ${r1.status}: ${text.slice(0, 300)}`);
  }

  const challenge = await r1.json();
  const paymentId = challenge.payment_id;
  const amountUsdc = challenge.amount_usdc;

  // Validate free tool
  const isFree = parseFloat(amountUsdc) === 0;
  if (!isFree) {
    throw new McpError(
      ErrorCode.InvalidRequest,
      `'${toolName}' is a paid tool (${amountUsdc} USDC). ` +
      `MCP-1 supports the 17 free tools only. For paid tools, use the agentpay-x402 Python SDK.`,
    );
  }

  // Step 2: retry with free proof (no on-chain settlement)
  const txHash = `free:${paymentId}`;
  const xPayment = `tx_hash=${txHash},from=${AGENT_ADDRESS},id=${paymentId}`;

  const r2 = await fetch(url, {
    method: 'POST',
    headers: {
      ...baseHeaders,
      'X-Payment': xPayment,
      'X-Agent-Address': AGENT_ADDRESS,
    },
    body,
    signal: AbortSignal.timeout(45_000),
  });

  if (!r2.ok) {
    const text = await r2.text();
    throw new Error(`Tool call failed after free proof: ${r2.status} ${text.slice(0, 300)}`);
  }

  const result = await r2.json();
  return result.result ?? result;
}

// ── Route tool — buyer-side x402 routing (MCP-2) ─────────────────────────────

const BAZAAR_URL = 'https://api.cdp.coinbase.com/platform/v2/x402/discovery/search';
const DEFAULT_BUDGET = 0.01;

// Known stub-factory payTo addresses (from 2026-06-03 competitor scan).
// Same wallet behind ≥3 distinct service names = factory.
const KNOWN_FACTORY_PREFIXES = ['0x2bb72231eed3']; // Orbis

function fmtPrice(usd) {
  if (usd == null) return '?';
  return '$' + usd.toFixed(6).replace(/\.?0+$/, '');
}

async function bazaarSearch(need) {
  const url = `${BAZAAR_URL}?query=${encodeURIComponent(need)}`;
  const resp = await fetch(url, {
    headers: { 'User-Agent': USER_AGENT, 'Accept': 'application/json' },
    signal: AbortSignal.timeout(25_000),
  });
  if (!resp.ok) throw new Error(`Bazaar search → ${resp.status}`);
  return resp.json();
}

function discover(data) {
  const out = [];
  const seen = new Set();

  for (const r of (data.resources || [])) {
    const res = r.resource;
    const rd = (res && typeof res === 'object') ? res : {};
    const url = (typeof res === 'string') ? res : (rd.url || '');
    if (!url || seen.has(url)) continue;
    seen.add(url);

    const accepts = r.accepts || rd.accepts || [{}];
    const a = accepts[0] || {};
    const amountRaw = parseInt(a.amount || '0', 10);
    const priceUsd = isNaN(amountRaw) ? null : amountRaw / 1_000_000;

    const ext = ((r.extensions || rd.extensions || {}).bazaar) || {};
    const outSchema = a.outputSchema || ext?.info?.output || ext?.schema;
    const hasSchema = !!outSchema && JSON.stringify(outSchema) !== '{}';

    const q = r.quality || {};
    const calls30d = parseInt(q.l30DaysTotalCalls || 0, 10) || 0;
    const payers30d = parseInt(q.l30DaysUniquePayers || 0, 10) || 0;

    out.push({
      name: r.serviceName || rd.serviceName || url.split('/').pop() || url,
      url,
      priceUsd,
      network: a.network || '',
      payTo: (a.payTo || '').toLowerCase(),
      asset: a.asset || '',
      amount: a.amount || '0',
      tags: r.tags || rd.tags || [],
      hasSchema,
      calls30d,
      payers30d,
      lastCalled: q.lastCalledAt || null,
      // store accepts[0] for ready-to-pay details
      acceptsEntry: a,
    });
  }
  return out;
}

function recencyDays(iso) {
  if (!iso) return null;
  try {
    const dt = new Date(iso);
    return Math.floor((Date.now() - dt.getTime()) / 86_400_000);
  } catch {
    return null;
  }
}

function decide(cands, budget) {
  // Factory detection: count distinct service names per payTo wallet
  const namesPerPayTo = {};
  for (const c of cands) {
    if (c.payTo) {
      if (!namesPerPayTo[c.payTo]) namesPerPayTo[c.payTo] = new Set();
      namesPerPayTo[c.payTo].add(c.name);
    }
  }

  const scored = [];
  for (const c of cands) {
    const flags = [];
    let dropped = false;
    let dropReason = '';

    // Stage 2 — junk filter: no usable schema = stub
    if (!c.hasSchema) {
      dropped = true;
      dropReason = 'no usable schema (stub)';
    }

    // Factory fingerprint: ≥3 DISTINCT names behind one payTo, or known factory wallet
    const isFactory = (namesPerPayTo[c.payTo]?.size >= 3) ||
      KNOWN_FACTORY_PREFIXES.some(p => c.payTo.startsWith(p));
    if (isFactory) flags.push('factory');

    // Budget gate
    if (!dropped) {
      if (c.priceUsd == null) {
        dropped = true;
        dropReason = 'no usable price';
      } else if (c.priceUsd > budget) {
        dropped = true;
        dropReason = `${fmtPrice(c.priceUsd)} > budget ${fmtPrice(budget)}`;
      }
    }

    // Stage 3 — usage quality score (Bazaar cold-start signal)
    const days = recencyDays(c.lastCalled);
    let quality = c.payers30d * 3 + c.calls30d;
    if (days != null && days <= 7) quality += 5;  // recency bonus: recently used = alive
    if (isFactory) quality = Math.floor(quality / 4); // heavy downrank
    if (c.payers30d === 0 && c.calls30d === 0) flags.push('unproven(0/0)');

    scored.push({ ...c, flags, dropped, dropReason, quality, recDays: days });
  }

  // Stage 4 — rank survivors: quality desc, then price asc
  const survivors = scored.filter(s => !s.dropped);
  survivors.sort((a, b) => (b.quality - a.quality) || (a.priceUsd - b.priceUsd));

  const recommendation = survivors[0] || null;
  return { scored, survivors, recommendation };
}

async function routeTool(need, budget) {
  const data = await bazaarSearch(need);
  const cands = discover(data);
  const { scored, survivors, recommendation } = decide(cands, budget);

  // Build the ranked list for the response
  const rankedList = scored
    .sort((a, b) => {
      if (a.dropped !== b.dropped) return a.dropped ? 1 : -1;
      return b.quality - a.quality;
    })
    .map(s => ({
      name: s.name,
      url: s.url,
      price_usd: s.priceUsd,
      network: s.network,
      calls_30d: s.calls30d,
      payers_30d: s.payers30d,
      last_called_days_ago: s.recDays,
      has_schema: s.hasSchema,
      flags: s.flags,
      dropped: s.dropped,
      drop_reason: s.dropReason || null,
      quality_score: s.quality,
    }));

  let rec = null;
  if (recommendation) {
    const why = [
      'real schema',
      `${recommendation.payers30d} unique payers / ${recommendation.calls30d} calls in 30d`,
      recommendation.recDays != null ? `used ${recommendation.recDays}d ago` : null,
      'fits budget',
      'price-tiebroken among quality peers',
    ].filter(Boolean).join('; ');

    rec = {
      name: recommendation.name,
      url: recommendation.url,
      price_usd: recommendation.priceUsd,
      network: recommendation.network,
      why,
      ready_to_pay: {
        url: recommendation.url,
        accepts: recommendation.acceptsEntry,
      },
    };
  }

  return {
    need,
    budget,
    total_found: cands.length,
    survivors: survivors.length,
    ranked_candidates: rankedList,
    recommendation: rec,
    note: 'Advise-only — peer-to-peer: the agent pays the chosen provider directly via x402. Use agentpay-x402 SDK (pip install agentpay-x402) to settle with the ready_to_pay details above.',
  };
}

// ── MCP Server ────────────────────────────────────────────────────────────────

const server = new Server(
  { name: 'agentpay', version: VERSION },
  { capabilities: { tools: {} } },
);

const ROUTE_TOOL_DEF = {
  name: 'route',
  description: [
    'Find and judge the best paid x402 tool for a need, within a budget.',
    'Discovers across Coinbase Bazaar, drops stubs (no schema / factory clones),',
    'ranks survivors by real usage (unique payers × 3 + calls + recency bonus),',
    'enforces the budget, price-tiebreaks quality-equal candidates.',
    'Returns a ranked candidate list + a recommendation + ready-to-pay details',
    '(the chosen provider URL and x402 accepts entry) so the agent can settle via',
    'the agentpay-x402 SDK. Advise-only — no payment happens here.',
    '',
    'Use when: "which x402 tool for X", "find a paid API for X under $Y",',
    '"compare these providers", or whenever an agent needs a paid capability',
    'and must choose a provider without overpaying or hitting a stub.',
  ].join(' '),
  inputSchema: {
    type: 'object',
    properties: {
      need: {
        type: 'string',
        description: 'What capability you need, e.g. "funding rates", "token security", "DeFi TVL"',
      },
      budget: {
        type: 'number',
        description: 'Maximum USDC per call (default 0.01). Tools priced above this are excluded.',
        default: DEFAULT_BUDGET,
      },
    },
    required: ['need'],
  },
};

const ESTIMATE_PLAN_TOOL_DEF = {
  name: 'estimate_plan',
  description: [
    'Price a multi-tool plan BEFORE spending anything. Submits the tool calls',
    'an agent intends to make to the gateway\'s free /v1/plan/estimate and',
    'returns per-step cost, total, a fits-budget verdict, and a cheaper',
    'alternative per paid step. No payment, no wallet needed.',
    '',
    'Use when: planning a multi-step task with paid tools, "what would this',
    'cost", "does this plan fit my budget", or before committing a Session cap.',
  ].join(' '),
  inputSchema: {
    type: 'object',
    properties: {
      steps: {
        type: 'array',
        description: 'Tool names to price, in order, e.g. ["token_price", "dune_query", "session_create"]',
        items: { type: 'string' },
      },
      budget: {
        type: 'number',
        description: 'Optional USDC budget for the fits_budget verdict, e.g. 0.05',
      },
    },
    required: ['steps'],
  },
};

async function estimatePlanTool(steps, budget) {
  const body = { steps: steps.map((t) => ({ tool: t })) };
  if (typeof budget === 'number') body.budget = String(budget);
  const res = await fetch(`${GATEWAY_URL}/v1/plan/estimate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'User-Agent': USER_AGENT },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error(`gateway returned ${res.status}`);
  return res.json();
}

server.setRequestHandler(ListToolsRequestSchema, async () => {
  const tools = await getTools();

  const gatewayTools = tools.map((t) => {
    let description = t.description ?? '';
    if (t.use_when) description += `\n\nUse when: ${t.use_when}`;
    if (t.returns) description += `\nReturns: ${t.returns}`;
    if (t.response_example) {
      description += `\nExample response: ${JSON.stringify(t.response_example)}`;
    }
    description += `\n\nPrice: $${t.price_usdc} USDC per call`;

    return {
      name: t.name,
      description,
      inputSchema: t.parameters ?? { type: 'object', properties: {} },
    };
  });

  return { tools: [...gatewayTools, ROUTE_TOOL_DEF, ESTIMATE_PLAN_TOOL_DEF] };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args = {} } = request.params;

  if (name === 'estimate_plan') {
    try {
      const steps = args.steps;
      if (!Array.isArray(steps) || steps.length === 0) {
        throw new McpError(ErrorCode.InvalidParams, '`steps` is required and must be a non-empty array of tool names');
      }
      const result = await estimatePlanTool(steps, args.budget);
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (err) {
      if (err instanceof McpError) throw err;
      return {
        content: [{ type: 'text', text: `AgentPay estimate_plan error: ${err.message}` }],
        isError: true,
      };
    }
  }

  // Handle route tool separately
  if (name === 'route') {
    try {
      const need = args.need;
      if (!need || typeof need !== 'string') {
        throw new McpError(ErrorCode.InvalidParams, '`need` is required and must be a string');
      }
      const budget = typeof args.budget === 'number' ? args.budget : DEFAULT_BUDGET;
      const result = await routeTool(need, budget);
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (err) {
      if (err instanceof McpError) throw err;
      return {
        content: [{ type: 'text', text: `AgentPay route error: ${err.message}` }],
        isError: true,
      };
    }
  }

  try {
    const result = await callTool(name, args);
    const text = JSON.stringify(result, null, 2);
    return { content: [{ type: 'text', text }] };
  } catch (err) {
    if (err instanceof McpError) throw err;
    return {
      content: [{ type: 'text', text: `AgentPay error calling '${name}': ${err.message}` }],
      isError: true,
    };
  }
});

// ── Entry point ───────────────────────────────────────────────────────────────

async function main() {
  // Pre-fetch tools so the first list_tools responds instantly
  try {
    const tools = await getTools();
    log(`AgentPay MCP v${VERSION}: loaded ${tools.length} tools from ${GATEWAY_URL} (+ route)`);
  } catch (err) {
    log(`AgentPay MCP v${VERSION}: could not pre-fetch tools (${err.message}) — will retry on first request`);
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);
  log('AgentPay MCP: server ready (stdio)');
}

main().catch((err) => {
  log(`AgentPay MCP: fatal error: ${err.message}`);
  process.exit(1);
});
