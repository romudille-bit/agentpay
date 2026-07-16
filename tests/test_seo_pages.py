"""
test_seo_pages.py — pins the SEO / AI-search discovery surfaces (2026-07-16).

Contract under test:

  1. /tools and /tools/{name} content-negotiate: an explicit text/html
     Accept (browser/crawler) gets a server-rendered, indexable page;
     everything else (Accept: */*, application/json — agents, the SDK,
     every pre-existing test) keeps the JSON contract byte-identical.
  2. The HTML pages carry what a search engine needs in the raw response:
     <title>, meta description, canonical, JSON-LD.
  3. /indexnow.txt serves the INDEXNOW_KEY (404 unconfigured).
  4. The landing page emits Google/Bing verification meta only when set.
  5. /probes seeds #board server-side with links to /s/ pages, so the
     per-service SEO pages are internally linked, not sitemap-only.
"""

import pytest

HTML_ACCEPT = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}


class TestToolPageNegotiation:
    def test_default_accept_stays_json(self, client):
        r = client.get("/tools/token_price")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["name"] == "token_price"

    def test_explicit_json_accept_stays_json(self, client):
        r = client.get("/tools/token_price",
                       headers={"Accept": "application/json"})
        assert r.headers["content-type"].startswith("application/json")

    def test_browser_accept_gets_html(self, client):
        r = client.get("/tools/token_price", headers=HTML_ACCEPT)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "<title>token_price" in r.text
        assert 'name="description"' in r.text
        assert 'rel="canonical"' in r.text
        assert "application/ld+json" in r.text
        # Vary: Accept — caches must not serve HTML to a JSON client
        assert "Accept" in r.headers.get("vary", "")

    def test_free_tool_page_says_free(self, client):
        r = client.get("/tools/token_price", headers=HTML_ACCEPT)
        assert "Free" in r.text
        assert "no API key" in r.text

    def test_paid_tool_page_shows_price_and_x402(self, client):
        r = client.get("/tools/pre_trade_check", headers=HTML_ACCEPT)
        assert "$0.01 USDC" in r.text
        assert "402" in r.text

    def test_alias_resolves_to_html_page(self, client):
        # dex_liquidity is a legacy alias — HTML negotiation must survive it
        r = client.get("/tools/dex_liquidity", headers=HTML_ACCEPT)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")

    def test_unknown_tool_404s_for_html_too(self, client):
        r = client.get("/tools/not_a_tool", headers=HTML_ACCEPT)
        assert r.status_code == 404


class TestToolsIndexNegotiation:
    def test_default_accept_stays_json(self, client):
        r = client.get("/tools")
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["count"] > 0

    def test_browser_accept_gets_html_directory(self, client):
        r = client.get("/tools", headers=HTML_ACCEPT)
        assert r.headers["content-type"].startswith("text/html")
        assert "ItemList" in r.text                       # JSON-LD directory
        assert '/tools/verified_route"' in r.text          # links every tool page
        assert 'rel="canonical"' in r.text

    def test_category_filter_still_json(self, client):
        r = client.get("/tools?category=defi")
        assert r.headers["content-type"].startswith("application/json")
        assert all(t["category"] == "defi" for t in r.json()["tools"])


class TestIndexNow:
    def test_404_when_unconfigured(self, client):
        from gateway.config import settings
        assert settings.INDEXNOW_KEY == ""
        assert client.get("/indexnow.txt").status_code == 404

    def test_serves_key_when_configured(self, client, monkeypatch):
        from gateway.config import settings
        monkeypatch.setattr(settings, "INDEXNOW_KEY", "abc123deadbeef")
        r = client.get("/indexnow.txt")
        assert r.status_code == 200
        assert r.text == "abc123deadbeef"
        assert r.headers["content-type"].startswith("text/plain")


class TestVerificationMeta:
    def test_absent_by_default(self, client):
        r = client.get("/", headers=HTML_ACCEPT)
        assert "google-site-verification" not in r.text
        assert "msvalidate.01" not in r.text

    def test_emitted_when_configured(self, client, monkeypatch):
        from gateway.config import settings
        monkeypatch.setattr(settings, "GOOGLE_SITE_VERIFICATION", "gtok123")
        monkeypatch.setattr(settings, "BING_SITE_VERIFICATION", "btok456")
        r = client.get("/", headers=HTML_ACCEPT)
        assert '<meta name="google-site-verification" content="gtok123">' in r.text
        assert '<meta name="msvalidate.01" content="btok456">' in r.text


class TestInternalLinking:
    def test_landing_links_tool_pages(self, client):
        r = client.get("/", headers=HTML_ACCEPT)
        assert '/tools/token_price"' in r.text

    def test_probes_board_is_server_seeded(self, client):
        # Placeholder must always be resolved — either to the /s/ link list
        # (Supabase up) or the loading message (Supabase down in tests).
        r = client.get("/probes")
        assert r.status_code == 200
        assert "PROBES_FALLBACK_PLACEHOLDER" not in r.text


class TestSitemap:
    def test_sitemap_includes_tool_pages(self, client):
        r = client.get("/sitemap.xml")
        assert r.status_code == 200
        assert "/tools/token_price</loc>" in r.text
        assert "/tools</loc>" in r.text
