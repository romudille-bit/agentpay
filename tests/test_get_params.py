"""
test_get_params.py — AGE-83.

A GET-served x402 resource takes its arguments in the URL. The SDK used to
call `client.get(url, headers=...)` and drop the caller's params entirely, so
every GET-served seller was paid and then called with NO arguments — it
answered with an error or an empty body and looked like a non-deliverer.

Live evidence: x402.shizu.me/pdf (GET ?url=) sat at delivery_rate 0.0 across
three paid prober probes while being a working service.
"""

from urllib.parse import parse_qs, urlsplit

from agentpay._wallet import _with_query


def q(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_params_reach_a_get_resource():
    out = _with_query("https://x402.shizu.me/pdf",
                      {"url": "https://w3.org/dummy.pdf"})
    assert q(out) == {"url": "https://w3.org/dummy.pdf"}


def test_existing_query_is_preserved():
    out = _with_query("https://a.com/t?key=abc", {"symbol": "ETH"})
    assert q(out) == {"key": "abc", "symbol": "ETH"}


def test_explicit_url_params_win_over_the_dict():
    # The caller who typed it into the URL was being explicit.
    out = _with_query("https://a.com/t?symbol=BTC", {"symbol": "ETH"})
    assert q(out)["symbol"] == "BTC"


def test_no_params_leaves_the_url_untouched():
    assert _with_query("https://a.com/t", {}) == "https://a.com/t"
    assert _with_query("https://a.com/t", None) == "https://a.com/t"


def test_values_are_encoded_not_concatenated():
    out = _with_query("https://a.com/t", {"q": "bitcoin etf inflows & more"})
    assert " " not in urlsplit(out).query
    assert q(out)["q"] == "bitcoin etf inflows & more"


def test_non_scalar_values_are_json_encoded():
    out = _with_query("https://a.com/t", {"messages": [{"role": "user"}]})
    assert q(out)["messages"] == '[{"role": "user"}]'


def test_none_values_are_dropped():
    assert q(_with_query("https://a.com/t", {"a": "1", "b": None})) == {"a": "1"}


def test_booleans_are_lowercase_not_python_case():
    assert q(_with_query("https://a.com/t", {"raw": True}))["raw"] == "true"


def test_path_and_scheme_are_untouched():
    out = _with_query("https://a.com/deep/path", {"x": "1"})
    parts = urlsplit(out)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "a.com", "/deep/path")


# ── end-to-end: the params must survive the paid retry ────────────────────────

from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from agentpay import Session

URL = "https://x402.shizu.me/pdf"
GET_402 = {
    "accepts": [{"network": "stellar:pubnet", "amount": "5000",
                 "payTo": "G" + "E" * 55, "scheme": "exact"}],
    # The seller declares GET — this is what makes the SDK switch methods.
    "extensions": {"bazaar": {"info": {
        "input": {"method": "GET", "queryParams": {"url": "https://e.com/f.pdf"}}}}},
}


def _wallet():
    w = MagicMock()
    w.public_key = "G" + "F" * 55
    w.network = "mainnet"
    w.base_address = None
    w.base_disabled_reason = None
    w.pay.return_value = {"success": True, "tx_hash": "hash" + "a" * 60}
    return w


def test_paid_get_retry_carries_the_params():
    """The regression that scored a working PDF service 0.0 three times."""
    s = Session(_wallet(), gateway_url="https://agentpay.tools", max_spend="1.00")
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(402, json=GET_402))
        route = respx.get(URL).mock(
            return_value=httpx.Response(200, json={"text": "Dummy PDF file"}))
        r = s.call(URL, {"url": "https://w3.org/dummy.pdf"})
    assert r["text"] == "Dummy PDF file"
    sent = route.calls.last.request.url
    assert sent.params["url"] == "https://w3.org/dummy.pdf"


def test_post_resources_still_send_a_json_body():
    post_402 = {"accepts": GET_402["accepts"]}          # no declared method
    s = Session(_wallet(), gateway_url="https://agentpay.tools", max_spend="1.00")
    with respx.mock:
        route = respx.post(URL).mock(side_effect=[
            httpx.Response(402, json=post_402),
            httpx.Response(200, json={"ok": True}),
        ])
        s.call(URL, {"symbol": "ETH"})
    import json as _json
    assert _json.loads(route.calls.last.request.content) == {"symbol": "ETH"}
