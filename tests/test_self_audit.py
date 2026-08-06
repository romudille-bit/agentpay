"""AGE-108: the self-audit must catch the two bugs that shipped unnoticed.

Injected fetchers — no network, no payments.
"""

import base64
import json

from agents.prober import self_audit


def _hdr(resource):
    """402s carry the resource block ONLY in the base64 PAYMENT-REQUIRED header."""
    if resource is None:
        return {}
    blob = base64.b64encode(json.dumps({"resource": resource}).encode()).decode()
    return {"PAYMENT-REQUIRED": blob}


def _fakes(tools, paid_402, unknown=404, wk=200):
    """Build (get, post) stubs. `paid_402` maps url -> 402 payload dict."""
    def get(url, timeout=15):
        if url.endswith("/tools"):
            return 200, json.dumps({"tools": tools})
        if self_audit.BOGUS_TOOL in url:
            return unknown, ""
        return wk, ""

    def post(url, body, timeout=20):
        p = paid_402.get(url)
        if not p:
            return 405, "", {}          # not a payable path (info page)
        return 402, json.dumps({"accepts": p["accepts"]}), _hdr(p["resource"])
    return get, post


def _ok_402(name, url):
    return {"resource": {"serviceName": name, "url": url},
            "accepts": [{"payTo": "0xabc", "network": "eip155:8453",
                         "amount": "10000"}]}


GOOD_TOOL = {"name": "verified_route", "active": True, "price_usdc": "0.01",
             "endpoint": "https://g.test/tools/verified_route/call",
             "description": "d", "parameters": {"type": "object"}}


def test_clean_surface_passes():
    t = dict(GOOD_TOOL)
    url = t["endpoint"]
    get, post = _fakes([t], {url: _ok_402("AgentPay x402 Trust Oracle", url)})
    r = self_audit.audit("https://g.test", get=get, post=post)
    assert r["ok"], r["failures"]
    assert "self-audit OK" in self_audit.summarize(r)


def test_catches_empty_discovery_field():
    """AGE-107: gas_tracker et al. served endpoint='' for weeks."""
    broken = {**GOOD_TOOL, "name": "gas_tracker", "endpoint": "",
              "price_usdc": "0"}
    get, post = _fakes([broken], {})
    r = self_audit.audit("https://g.test", get=get, post=post)
    assert not r["ok"]
    assert any("gas_tracker" in f and "endpoint" in f for f in r["failures"])
    assert "SELF-AUDIT FAILED" in self_audit.summarize(r)


def test_catches_disagreeing_payable_paths():
    """AGE-112 exactly as it shipped: session_create payable on two paths, the
    /tools one declaring its own url and no serviceName — so a settle there
    indexed a second, unnamed resource and never refreshed the real listing."""
    t = {**GOOD_TOOL, "name": "session_create",
         "endpoint": "https://g.test/v1/session/create"}
    tool_path = "https://g.test/tools/session_create/call"
    get, post = _fakes([t], {
        t["endpoint"]: _ok_402("AgentPay Spend Cap & Receipts", t["endpoint"]),
        tool_path: {"resource": {"url": tool_path},
                    "accepts": [{"payTo": "0xabc", "network": "eip155:8453",
                                 "amount": "10000"}]},
    })
    r = self_audit.audit("https://g.test", get=get, post=post)
    assert not r["ok"]
    assert any("declares no serviceName" in f for f in r["failures"])
    assert any("different resources" in f for f in r["failures"])


def test_catches_empty_accepts():
    """Issue #1 from the earlier external report: accepts[] null."""
    t = dict(GOOD_TOOL)
    get, post = _fakes([t], {t["endpoint"]: {
        "resource": {"serviceName": "x", "url": t["endpoint"]}, "accepts": []}})
    r = self_audit.audit("https://g.test", get=get, post=post)
    assert not r["ok"]
    assert any("accepts[] empty" in f for f in r["failures"])


def test_negative_control_unknown_tool_must_404():
    t = dict(GOOD_TOOL)
    get, post = _fakes([t], {t["endpoint"]: _ok_402("x", t["endpoint"])},
                       unknown=200)
    r = self_audit.audit("https://g.test", get=get, post=post)
    assert not r["ok"]
    assert any("expected 404" in f for f in r["failures"])


def test_well_known_surface_down_is_reported():
    t = dict(GOOD_TOOL)
    get, post = _fakes([t], {t["endpoint"]: _ok_402("x", t["endpoint"])}, wk=500)
    r = self_audit.audit("https://g.test", get=get, post=post)
    assert not r["ok"]
    assert any("/llms.txt returned 500" in f for f in r["failures"])


def test_free_tools_are_not_402_probed():
    """Only priced tools get an unpaid 402 probe; free ones would 200."""
    free = {**GOOD_TOOL, "name": "token_price", "price_usdc": "0"}
    get, post = _fakes([free], {})
    r = self_audit.audit("https://g.test", get=get, post=post)
    assert r["ok"], r["failures"]
    assert r["checks"]["canonical_402"] == []
