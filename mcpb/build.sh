#!/usr/bin/env bash
# Build agentpay.mcpb — a self-contained Claude Desktop extension (MCP Bundle).
#
# Single source of truth for the server is npm/bin/ (agentpay-mcp.js + its
# sibling modules, e.g. eip3009.js since 2.4.x); this script copies ALL of
# npm/bin/*.js in, installs the npm package's ACTUAL runtime dependencies
# (read from npm/package.json — do not hardcode the dep list: 2.4.x added
# @noble/curves + @noble/hashes and a hardcoded list shipped a bundle that
# crashed on launch with ERR_MODULE_NOT_FOUND), smoke-tests the staged
# server, and packs a .mcpb. For the Anthropic Connectors Directory
# (Desktop Extension path, AGE-137).
#
# Usage:  cd mcpb && ./build.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "1/4 — staging server (all npm/bin modules)"
rm -rf server node_modules *.mcpb package.json package-lock.json
mkdir -p server
cp ../npm/bin/*.js server/
mv server/agentpay-mcp.js server/index.js

echo "2/4 — bundling runtime deps from npm/package.json"
DEPS=$(node -e 'const d=require("../npm/package.json").dependencies||{};process.stdout.write(Object.entries(d).map(([k,v])=>`${k}@${v}`).join(" "))')
echo "     deps: $DEPS"
npm init -y >/dev/null 2>&1
# shellcheck disable=SC2086
npm install --no-audit --no-fund $DEPS >/dev/null

echo "3/4 — smoke test: server must start and answer initialize"
SMOKE=$( (printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"build-smoke","version":"0"}}}\n'; sleep 3) \
  | timeout 15 node server/index.js 2>/dev/null | head -c 2000 || true)
if ! printf '%s' "$SMOKE" | grep -q '"serverInfo"'; then
  echo "✗ smoke test FAILED — server did not answer initialize. Aborting pack." >&2
  exit 1
fi
echo "     ✓ initialize answered"

echo "4/4 — packing agentpay.mcpb"
# Prefer the official CLI (validates the manifest + spec-compliant packing).
# .mcpbignore keeps build tooling out of the bundle.
printf 'build.sh\nREADME.md\n.gitignore\n.mcpbignore\npackage.json\npackage-lock.json\n*.mcpb\n' > .mcpbignore
if npx -y @anthropic-ai/mcpb pack . agentpay.mcpb; then
  :
else
  # Fallback: a .mcpb is a zip of manifest.json + server/ + node_modules/.
  zip -qr agentpay.mcpb manifest.json server node_modules \
    -x '*/.DS_Store' 'node_modules/.package-lock.json'
fi

echo "✓ built $HERE/agentpay.mcpb"
echo "  Validate/install: double-click it in Claude Desktop, or 'npx -y @anthropic-ai/mcpb unpack agentpay.mcpb /tmp/mcpb-check'"
