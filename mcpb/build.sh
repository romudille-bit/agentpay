#!/usr/bin/env bash
# Build agentpay.mcpb — a self-contained Claude Desktop extension (MCP Bundle).
#
# Single source of truth for the server is npm/bin/agentpay-mcp.js; this script
# copies it in, bundles the one runtime dep (@modelcontextprotocol/sdk), and packs
# a .mcpb (zip). For the Anthropic Connectors Directory (Desktop Extension path).
#
# Usage:  cd mcpb && ./build.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "1/3 — staging server"
rm -rf server node_modules *.mcpb
mkdir -p server
cp ../npm/bin/agentpay-mcp.js server/index.js

echo "2/3 — bundling deps (@modelcontextprotocol/sdk)"
# Pin to the same major the npm package uses.
npm init -y >/dev/null 2>&1
npm install --no-audit --no-fund "@modelcontextprotocol/sdk@^1.12.0" >/dev/null

echo "3/3 — packing agentpay.mcpb"
if command -v mcpb >/dev/null 2>&1; then
  mcpb pack . agentpay.mcpb
else
  # Fallback: a .mcpb is just a zip of manifest.json + server/ + node_modules/.
  zip -qr agentpay.mcpb manifest.json server node_modules \
    -x '*/.DS_Store' 'node_modules/.package-lock.json'
fi

echo "✓ built $HERE/agentpay.mcpb"
echo "  Validate/install: double-click it in Claude Desktop, or 'mcpb info agentpay.mcpb'"
