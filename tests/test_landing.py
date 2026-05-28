"""
test_landing.py — Tests for the content-negotiated root endpoint.

GET / is dual-purpose: browsers (Accept: text/html) get an HTML landing page,
agents and API clients (Accept: application/json or none) get the JSON manifest.
HEAD / returns headers only — both content types respond with 200 so Bazaar's
quality-score check passes regardless of which Accept the indexer sends.

These tests pin the negotiation rules and the HTML's structural invariants
(tool count matches registry, no leaked Railway URLs, etc.) so a future
refactor doesn't silently break either path.
"""

import pytest

import registry
from gateway.landing import render_landing


def test_root_html_for_browser(client):
    """Browser with Accept: text/html → HTML 200 containing the hero copy."""
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    # Hero pins the current positioning: economic intelligence layer, 17 free
    # tools, zero cost to start. If this copy changes, the CMO skill, README,
    # and CLAUDE.md must move together.
    assert "economic intelligence" in body
    assert "AgentPay" in body
    # The quickstart snippet must be present
    assert "from agentpay import AgentWallet, Session" in body
    assert "spending_summary" in body


def test_root_json_for_agent(client):
    """Agent with Accept: application/json → JSON 200 with the manifest shape."""
    r = client.get("/", headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["name"] == "AgentPay"
    assert body["tools"] == len(registry.list_tools())
    assert "tools_endpoint" in body
    assert "discovery" in body


def test_root_json_when_no_accept_header(client):
    """No Accept header → defaults to JSON (legacy agent / curl behaviour)."""
    r = client.get("/")
    assert r.status_code == 200
    # curl with no Accept sends */* — should still return JSON, not HTML,
    # because we only return HTML when text/html is explicitly requested.
    assert r.headers["content-type"].startswith("application/json")


def test_root_json_when_browser_explicitly_requests_json(client):
    """Browser with Accept: text/html,application/json (json after html) →
    JSON. The check is 'text/html present AND application/json absent' so any
    mixed accept that includes JSON wins for the JSON path. This protects
    Postman/Bruno-style clients that send both."""
    r = client.get("/", headers={"Accept": "text/html, application/json"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_root_head_returns_200_no_body(client):
    """HEAD / → 200 with empty body. FastAPI handles HEAD by running GET and
    dropping the body, so this works for both negotiated paths. Critical for
    Bazaar / monitoring uptime checks."""
    r = client.head("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.content == b""


def test_landing_lists_all_active_tools():
    """Every active tool in the registry should appear in the rendered HTML.

    Pinning this catches the case where a tool ships in registry.py but the
    landing's tool list logic silently filters it out (e.g., wrong sort key,
    missing field).
    """
    tools = registry.list_tools()
    html = render_landing(tools, "https://agentpay.tools")
    active = [t for t in tools if t.active]
    for tool in active:
        assert tool.name in html, f"missing {tool.name} in landing HTML"
        # _price_label converts 0.000 → "Free"; paid tools show "$X.XXX"
        try:
            expected = "Free" if float(tool.price_usdc) == 0 else f"${tool.price_usdc}"
        except (ValueError, TypeError):
            expected = f"${tool.price_usdc}"
        assert expected in html, f"missing price label '{expected}' for {tool.name}"


def test_landing_uses_provided_gateway_url():
    """render_landing must substitute the gateway_url everywhere — no leftover
    placeholder, no hardcoded Railway hostname."""
    html = render_landing(registry.list_tools(), "https://agentpay.tools")
    assert "GATEWAY_URL_PLACEHOLDER" not in html
    assert "TOOLS_ROWS_PLACEHOLDER" not in html
    assert "https://agentpay.tools" in html
    # The old Railway hostname must not leak from anywhere.
    assert "gateway-production-2cc2" not in html


def test_landing_escapes_html_in_descriptions():
    """Defensive HTML-escape on tool descriptions. Registry data may someday
    come from Supabase and contain user-provided strings — we should never
    blindly inject them into the page."""
    from registry.registry import Tool
    poisoned = Tool(
        name="evil_tool",
        description='<script>alert("xss")</script>',
        endpoint="https://agentpay.tools/tools/evil",
        price_usdc="0.001",
        developer_address="GBAD",
        parameters={},
    )
    html = render_landing([poisoned], "https://agentpay.tools")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
