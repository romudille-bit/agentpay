"""test_connect_pages.py — connector install pages (/connect, /connect/<harness>)."""

from gateway.connect import HARNESSES


def test_index_lists_every_harness_and_the_url(client):
    r = client.get("/connect")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    for slug in HARNESSES:
        assert f"/connect/{slug}" in r.text
    assert "/mcp</code>" in r.text


def test_each_harness_page_renders_with_steps_and_schema(client):
    for slug, h in HARNESSES.items():
        r = client.get(f"/connect/{slug}")
        assert r.status_code == 200, slug
        assert h["name"] in r.text
        assert "/mcp</code>" in r.text
        assert '"@type":"HowTo"' in r.text
        assert f'<link rel="canonical" href="' in r.text
        assert "readOnlyHint" in r.text or "Paid verdicts" in r.text


def test_unknown_harness_404(client):
    assert client.get("/connect/copilot").status_code == 404


def test_sitemap_and_footer_carry_connect(client):
    xml = client.get("/sitemap.xml").text
    assert "/connect</loc>" in xml
    assert "/connect/chatgpt</loc>" in xml
    assert "/connect" in client.get("/guides").text
