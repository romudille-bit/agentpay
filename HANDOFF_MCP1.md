# Handoff — Build MCP-1: self-contained Node MCP server (free tools)

*Paste this whole file into a fresh session to build it. Context lives in the repo;
this brief is the task.*

## Goal

Replace the broken `@romudille/agentpay-mcp` npm package with a **self-contained, Node-native
MCP server** that bundles AgentPay's **17 free tools** and runs anywhere Node runs — no Python,
no repo checkout, no pip. `npx @romudille/agentpay-mcp` must work standalone.

This is **MCP-1** of the plan in `AUTONOMY_MCP.md` (read it first, plus `CLAUDE.md`). Scope is
**free tools only** — NO wallet, NO signing, NO payments. (Routing `route` = MCP-2; paid =
SDK-side, later.) Keyless.

## Why (the problem)

The published `@romudille/agentpay-mcp@1.1.0` ships only `npm/bin/agentpay-mcp.js`, a wrapper
that shells to the repo's Python `gateway/mcp_server.py` (looks for it at `../../gateway/...`
or cwd). Outside the repo it dies with "Could not find gateway/mcp_server.py". So the npm
package and the Glama listing are effectively broken for everyone, and the Claude Code plugin
ships with `plugins/agentpay/.mcp.json` = `{"mcpServers":{}}` (free tools deferred to this build).

## What to build

A Node MCP server (use `@modelcontextprotocol/sdk`, stdio transport) that:
1. On startup, fetches the tool list from the gateway (`GET https://agentpay.tools/tools` →
   `{"tools":[...]}`) and exposes each as an MCP tool (name, description, input schema). Do it
   dynamically so new tools appear without code changes (mirror the Python `gateway/mcp_server.py`).
2. On `tools/call`, runs the **x402 free-flow** against the gateway and returns the tool result.
3. Needs NO wallet. Free tools settle nothing.

### The x402 free-flow (canonical: `agentpay/_client.py`)
- `POST https://agentpay.tools/tools/{name}/call` with body
  `{"parameters": {...}, "agent_address": "<any id>"}`.
- Gateway replies `402` with JSON incl. `payment_id` and `amount_usdc` = `"0"` for free tools.
- Retry the same POST with headers:
  - `X-Payment: tx_hash=free:<payment_id>,from=<agent_address>,id=<payment_id>`
  - `X-Agent-Address: <agent_address>`
- Gateway returns `200` with `{"tool","result","payment"}`. Return `result` to the MCP caller.
- For the `agent_address`: mint/generate an ephemeral identity at startup (the Python server
  mints an ephemeral Stellar Keypair when no key is set — c90d954). For free calls the address is
  only used for logging, so any stable per-process id works; simplest is fine.

### Gotchas (learned)
- **User-Agent:** `agentpay.tools` is behind a CDN that 403s default/blank user-agents. Set an
  explicit `User-Agent` header (e.g. `agentpay-mcp/<version>`) on every request.
- Keep it thin and dependency-light. No Python. No reading repo files at runtime.

## Files to read first
- `AUTONOMY_MCP.md` — the full design (this is MCP-1; note keyless decision, build sequence).
- `CLAUDE.md` — project context, tools table (17 free + `session_create`), gateway URLs.
- `gateway/mcp_server.py` — the Python server to PORT to Node (free-flow + dynamic tools +
  ephemeral identity + network-mismatch handling). Match its behavior.
- `agentpay/_client.py` — canonical free-flow ($0 → `free:<id>` retry).
- `registry/registry.py` — tool names, descriptions, input params, `response_example` per tool.
- `npm/bin/agentpay-mcp.js` + `npm/package.json` — current broken wrapper to replace.
- `plugins/agentpay/.mcp.json` — wire the new server here when done.

## The 17 free tools (from registry)
url_reader, web_search, market_snapshot, token_price, gas_tracker, fear_greed_index,
token_market_data, wallet_balance, whale_activity, defi_tvl, token_security, open_interest,
orderbook_depth, crypto_news, funding_rates, yield_scanner, dune_query.
(`session_create` is the one PAID tool, $0.01 — EXCLUDE from MCP-1; it needs a wallet.)

## Deliverables
1. Node MCP server under `npm/` (self-contained; bundle everything; `bin` entry).
2. Bump `npm/package.json` version (e.g. 2.0.0 — it's a rewrite), keep bin name `agentpay-mcp`,
   set `files` so the server actually ships. Add `@modelcontextprotocol/sdk` dep.
3. Update `plugins/agentpay/.mcp.json` to launch it (`{"mcpServers":{"agentpay":{"command":"npx",
   "args":["-y","@romudille/agentpay-mcp"]}}}`). NOTE: `.mcp.json` is write-guarded by the file
   tools — edit it via the Bash/shell, not Write/Edit.
4. Update docs: `CLAUDE.md` Discovery/npm rows (no longer "broken"), `plugins/agentpay/README.md`
   (free tools restored), and the `agentpay-route` skill can mention the free tools exist.

## Testing (must do before publish)
- Local stdio handshake: spawn the server, send `initialize` → `notifications/initialized` →
  `tools/list` (expect ~17 tools) → `tools/call` for `token_price {"symbol":"ETH"}` and confirm
  a real price comes back. Test against the LIVE gateway (free, no wallet, no spend).
- `npx`-from-anywhere test: `npm pack`, install the tarball in a tmp dir, run it OUTSIDE the
  repo, confirm it starts and serves tools (this is the exact failure mode of the old package).
- Then publish: `npm publish` (bumped version). Verify `npx -y @romudille/agentpay-mcp` works
  from `/tmp`.

## Constraints
- Keyless. Free tools only. Self-contained (no Python, no repo dependency).
- Git identity: commit/push as `romudille` / `romudille@gmail.com` (GitHub `romudille-bit`).
- Verify functionally — don't trust counters; actually call a tool and see data.

## Definition of done
`npx -y @romudille/agentpay-mcp` from an empty directory starts an MCP server exposing the 17
free tools, and `token_price` returns a live price — with no wallet, no Python, no repo. Plugin
`.mcp.json` points at it; a fresh plugin install gets working free tools alongside the
`agentpay-route` skill.
