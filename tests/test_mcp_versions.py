"""test_mcp_versions.py — one MCP server version everywhere it is published."""

import json
import re
from pathlib import Path

NPM = Path(__file__).resolve().parents[1] / "npm"


def test_npm_registry_binary_and_server_card_agree(client):
    version = json.loads((NPM / "package.json").read_text())["version"]
    lock = json.loads((NPM / "package-lock.json").read_text())
    server = json.loads((NPM / "server.json").read_text())
    binary = re.search(r"const VERSION = '([^']+)'", (NPM / "bin" / "agentpay-mcp.js").read_text())
    card = client.get("/.well-known/mcp/server-card.json").json()
    found = {
        "package.json": version,
        "package-lock": lock["version"],
        "package-lock root": lock["packages"][""]["version"],
        "server.json": server["version"],
        "server.json npm package": server["packages"][0]["version"],
        "binary": binary.group(1),
        "server card": card["serverInfo"]["version"],
    }
    assert set(found.values()) == {version}, found
