# AgentPay — MCP Bundle (Claude Desktop extension)

`agentpay.mcpb` packages the AgentPay MCP server as a single-click **Claude Desktop extension** for the Anthropic **Connectors Directory** (Desktop Extension path). Same server as `npx @romudille/agentpay-mcp`, bundled with its one dependency so it runs offline with the Node runtime that ships with Claude Desktop.

## Build

```bash
cd mcpb
./build.sh        # → agentpay.mcpb
```

The source of truth for the server is `../npm/bin/agentpay-mcp.js`; `build.sh` copies it into `server/index.js`, bundles `@modelcontextprotocol/sdk`, and packs the `.mcpb`. Build outputs (`server/`, `node_modules/`, `*.mcpb`) are gitignored.

## What's inside

- `manifest.json` — MCPB manifest (manifest_version 0.2): server config, tool metadata, `privacy_policies` (https://agentpay.tools/privacy), platform/runtime compatibility.
- `server/index.js` — the Node stdio MCP server (17 free tools + `verified_route` thin preview + `route` + `estimate_plan`; tools carry directory-ready annotations).

## Submit

1. Build (above).
2. Submit `agentpay.mcpb` via the Desktop Extension form: https://clau.de/desktop-extention-submission
3. Requirements met: tool annotations ✓ (v2.3.0), privacy policy ✓ (https://agentpay.tools/privacy).

## Privacy Policy

AgentPay does not collect names, emails, or other personal identifiers; the server can run keyless. Full policy: **https://agentpay.tools/privacy**
