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
    assert "from agentpay import quickstart" in body
    assert "spending_summary" in body
    # The agent-install surfaces (plugin + MCP) must be present
    assert "plugin marketplace add romudille-bit/agentpay" in body
    assert "npx -y @romudille/agentpay-mcp" in body


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


def test_root_markdown_for_agents(client):
    """Accept: text/markdown → the live /llms.txt document as text/markdown.

    Origin-side equivalent of Cloudflare's paid "Markdown for Agents" feature:
    agents that negotiate markdown get the LLM-readable service description
    instead of the HTML landing or the JSON manifest."""
    r = client.get("/", headers={"Accept": "text/markdown"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# AgentPay")
    assert "quickstart" in r.text
    # Same document as /llms.txt (single source of truth).
    assert r.text == client.get("/llms.txt").text
    # Markdown wins even in a mixed Accept that also lists html/json.
    r2 = client.get("/", headers={"Accept": "text/markdown, text/html"})
    assert r2.headers["content-type"].startswith("text/markdown")


def test_root_vary_accept(client):
    """Root varies by Accept — the Vary header must say so on every path,
    or an edge cache could serve markdown to a browser (and vice versa)."""
    for accept in ("text/html", "application/json", "text/markdown"):
        r = client.get("/", headers={"Accept": accept})
        assert r.headers.get("vary") == "Accept", accept


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


# ── Startup config validation (Phase 2.4) ────────────────────────────────────

class TestValidateConfig:

    def test_mainnet_without_keys_refuses_boot(self, monkeypatch):
        from gateway import main as gw_main
        monkeypatch.setattr(gw_main.settings, "STELLAR_NETWORK", "mainnet")
        monkeypatch.setattr(gw_main.settings, "GATEWAY_PUBLIC_KEY", "")
        monkeypatch.setattr(gw_main.settings, "GATEWAY_SECRET_KEY", "")
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="GATEWAY_PUBLIC_KEY"):
            gw_main._validate_config()

    def test_testnet_without_keys_boots(self, monkeypatch):
        from gateway import main as gw_main
        monkeypatch.setattr(gw_main.settings, "STELLAR_NETWORK", "testnet")
        monkeypatch.setattr(gw_main.settings, "GATEWAY_SECRET_KEY", "")
        gw_main._validate_config()  # no raise

    def test_mainnet_with_keys_boots(self, monkeypatch):
        from gateway import main as gw_main
        monkeypatch.setattr(gw_main.settings, "STELLAR_NETWORK", "mainnet")
        monkeypatch.setattr(gw_main.settings, "GATEWAY_PUBLIC_KEY", "G" + "A" * 55)
        monkeypatch.setattr(gw_main.settings, "GATEWAY_SECRET_KEY", "S" + "A" * 55)
        gw_main._validate_config()  # no raise


def test_landing_links_proof_pages(client):
    """Cleanup pass 2026-07-11: /probes + /ledger must be reachable from the
    landing page (nav, live-proof section, footer)."""
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert html.count("/probes") >= 2
    assert html.count("/ledger") >= 2
    assert "Live proof" in html
    assert 'name="twitter:card"' in html


def test_sitemap_covers_public_pages(client):
    xml = client.get("/sitemap.xml").text
    for path in ("/probes", "/ledger", "/radar", "/privacy"):
        assert path in xml, path


def test_probes_page_has_seo_and_ledger_callout(client):
    html = client.get("/probes").text
    assert 'name="description"' in html
    assert 'rel="canonical"' in html
    assert "score itself" in html            # self-exclusion stated on page
    assert "AgentPay's own tools" in html    # self section, receipt-evidenced
    assert "/ledger" in html


def test_og_image_served_and_referenced(client):
    r = client.get("/og.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert "/og.png" in html
    assert "summary_large_image" in html


# ── Agent-readiness endpoints (Cloudflare agent-ready checklist, 2026-07-12) ──

def test_root_link_header(client):
    r = client.get("/", headers={"Accept": "text/html"})
    link = r.headers.get("link", "")
    assert 'rel="api-catalog"' in link
    assert 'rel="service-desc"' in link
    # JSON negotiation carries the same header
    assert 'rel="api-catalog"' in client.get("/").headers.get("link", "")


def test_api_catalog_linkset(client):
    r = client.get("/.well-known/api-catalog")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/linkset+json")
    ls = r.json()["linkset"][0]
    assert ls["service-desc"][0]["href"].endswith("/openapi.json")
    assert ls["status"][0]["href"].endswith("/health")


def test_auth_md(client):
    r = client.get("/auth.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "no OAuth" in r.text.replace("**", "")
    assert "/v1/agent/register" in r.text


def test_mcp_server_card(client):
    r = client.get("/.well-known/mcp/server-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["serverInfo"]["name"] == "agentpay-mcp"
    assert card["transport"]["command"] == "npx"


def test_robots_welcoming_content_signal(client):
    txt = client.get("/robots.txt").text
    assert "Content-Signal: search=yes, ai-train=yes, ai-input=yes" in txt
