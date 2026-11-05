"""/.well-known/x402list.txt — the ownership proof x402-list.com asks for."""
def test_x402list_token_is_404_until_set(client, monkeypatch):
    from gateway.config import settings
    monkeypatch.setattr(settings, "X402LIST_TOKEN", "")
    assert client.get("/.well-known/x402list.txt").status_code == 404
    monkeypatch.setattr(settings, "X402LIST_TOKEN", " abc123 ")
    r = client.get("/.well-known/x402list.txt")
    assert r.status_code == 200 and r.text == "abc123\n"
    assert r.headers["content-type"].startswith("text/plain")
