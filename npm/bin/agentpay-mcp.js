#!/usr/bin/env node
/**
 * agentpay-mcp.js — AgentPay MCP Server (Node-native, self-contained)
 *
 * Exposes AgentPay's 17 free tools as MCP tools via stdio transport.
 * No Python, no repo checkout, no wallet — runs anywhere with Node ≥ 18.
 *
 * x402 free-flow (mirrors agentpay/_client.py):
 *   1. POST /tools/{name}/call → 402 with payment_id, amount_usdc = "0"
 *   2. Retry with X-Payment: tx_hash=free:<id>,from=<addr>,id=<id>
 *   3. Return result["result"] to the MCP caller
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

const VERSION = '2.0.0';
const GATEWAY_URL = (process.env.AGENTPAY_GATEWAY_URL || 'https://agentpay.tools').replace(/\/$/, '');

// Ephemeral agent identity — free calls use it only for the `from=` field in
// X-Payment (gateway logs only). A UUID is simpler and equally valid here.
const AGENT_ADDRESS = `mcp-free-${randomUUID()}`;

const USER_AGENT = `agentpay-mcp/${VERSION}`;

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

// ── MCP Server ────────────────────────────────────────────────────────────────

const server = new Server(
  { name: 'agentpay', version: VERSION },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  const tools = await getTools();

  return {
    tools: tools.map((t) => {
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
    }),
  };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args = {} } = request.params;

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
    log(`AgentPay MCP v${VERSION}: loaded ${tools.length} tools from ${GATEWAY_URL}`);
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
