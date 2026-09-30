"""
test_mcp_proxy.py — /mcp reverse proxy to the remote MCP service (AGE-127).

A real local HTTP server stands in for the Node service so the relay is
tested end to end: status, body, SSE content-type and the MCP headers must
come through; hop headers must not; an unset upstream answers 503.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gateway.config import settings


class _Upstream(BaseHTTPRequestHandler):
    seen: list = []

    def _record(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        _Upstream.seen.append({"method": self.command, "path": self.path,
                               "headers": dict(self.headers), "body": body})
        return body

    def do_POST(self):
        body = self._record()
        payload = json.loads(body or b"{}")
        if payload.get("method") == "slow-sse":
            out = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("mcp-session-id", "sess-1")
            self.send_header("x-powered-by", "should-not-relay")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        out = json.dumps({"echo": payload}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._record()
        self.send_response(405)
        self.send_header("allow", "POST")
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *a):  # keep pytest output clean
        pass


@pytest.fixture
def upstream(monkeypatch):
    _Upstream.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Upstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(settings, "MCP_UPSTREAM_URL", url)
    yield url
    srv.shutdown()


def test_unset_upstream_is_503_with_local_hint(client, monkeypatch):
    monkeypatch.setattr(settings, "MCP_UPSTREAM_URL", "")
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 503
    assert "npx" in r.json()["local"]


def test_post_relays_body_headers_and_client_ip(client, upstream):
    msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
    r = client.post("/mcp", json=msg, headers={
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
        "cookie": "secret=1",
    })
    assert r.status_code == 200
    assert r.json() == {"echo": msg}
    up = _Upstream.seen[-1]
    assert up["path"] == "/mcp"
    assert json.loads(up["body"]) == msg
    assert up["headers"].get("mcp-protocol-version") == "2025-06-18"
    assert "cookie" not in {k.lower() for k in up["headers"]}
    assert up["headers"].get("x-forwarded-for")


def test_sse_stream_and_mcp_headers_come_back(client, upstream):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "slow-sse"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["mcp-session-id"] == "sess-1"
    assert "x-powered-by" not in r.headers
    assert '"ok":true' in r.text


def test_get_passes_through_405(client, upstream):
    r = client.get("/mcp")
    assert r.status_code == 405
    assert r.headers.get("allow") == "POST"


def test_unreachable_upstream_is_502(client, monkeypatch):
    monkeypatch.setattr(settings, "MCP_UPSTREAM_URL", "http://127.0.0.1:9")  # nothing listens
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 502


def test_server_card_advertises_remote_when_wired(client, upstream):
    card = client.get("/.well-known/mcp/server-card.json").json()
    types = [t["type"] for t in card["transports"]]
    assert types == ["streamable-http", "stdio"]
    assert card["transport"]["url"].endswith("/mcp")
    assert "/mcp" in client.get("/llms.txt").text


def test_server_card_stdio_only_when_not_wired(client, monkeypatch):
    monkeypatch.setattr(settings, "MCP_UPSTREAM_URL", "")
    card = client.get("/.well-known/mcp/server-card.json").json()
    assert [t["type"] for t in card["transports"]] == ["stdio"]
    assert card["transport"]["command"] == "npx"
    assert "Remote MCP" not in client.get("/llms.txt").text
