"""AGE-113: the Bazaar listing keepalive.

Measured 2026-08-09: session_create fell out of the Bazaar index within three
days of its AGE-111 re-index, because nothing pays for it. verified_route and
pre_trade_check survived on a real customer's traffic. These tests pin the two
properties that matter — we pay when the listing is genuinely gone, and we do
NOT pay for any other reason, least of all a flaky index.

No network, no wallet: the search and the session are injected.
"""

from decimal import Decimal

import pytest

from agents.analyst import listing_keepalive as ka

SESSION_URL = ka.SESSION_RESOURCE_URL


def _payload(*urls, obj_shape=False):
    """Bazaar returns `resource` as a bare string OR an object. Build either."""
    return {"resources": [
        {"resource": ({"url": u, "serviceName": "x"} if obj_shape else u)}
        for u in urls]}


class _Session:
    """Minimal stand-in for the analyst's live Session."""

    def __init__(self, exc=None, over_cap=False, cost=Decimal("0.01")):
        self.exc, self.over_cap, self.cost = exc, over_cap, cost
        self.calls = []

    def tool_cost_usd(self, _tool):
        return self.cost

    def would_exceed(self, _amount):
        return self.over_cap

    def call(self, tool, params=None):
        self.calls.append((tool, params))
        if self.exc:
            raise self.exc
        return type("R", (), {"tx": "0xdeadbeef", "data": {"ok": True}})()


def _logs():
    out = []
    return out, out.append


# ── the shape-tolerance the index actually requires ──────────────────────────

def test_indexed_url_parsing_handles_both_resource_shapes():
    for shape in (False, True):
        p = _payload(SESSION_URL, "https://other.example/x", obj_shape=shape)
        assert ka.is_indexed(p), f"obj_shape={shape}"
    assert not ka.is_indexed(_payload("https://other.example/x"))


def test_trailing_slash_is_not_a_miss():
    """A trailing slash must not read as absence and buy a pointless settle."""
    assert ka.is_indexed(_payload(SESSION_URL + "/"))


def test_is_indexed_parses_empty_payloads_as_absent():
    """Pure parsing: is_indexed on an empty payload is False. Whether that
    triggers a settle is keepalive()'s decision — see the fail-closed tests."""
    assert not ka.is_indexed({"resources": []})
    assert not ka.is_indexed({})


# ── does it pay at the right times, and only then? ───────────────────────────

def test_indexed_listing_costs_nothing():
    s = _Session()
    out, log = _logs()
    r = ka.keepalive(s, log, search=lambda q: _payload(SESSION_URL))
    assert r == {"ran": True, "indexed": True, "settled": False,
                 "reason": "already indexed"}
    assert s.calls == [], "paid for a listing that was already there"


def test_missing_listing_is_refreshed():
    """The AGE-113 case exactly: brand query returns our other two tools and
    not the session resource."""
    s = _Session()
    out, log = _logs()
    r = ka.keepalive(s, log, search=lambda q: _payload(
        "https://agentpay.tools/tools/pre_trade_check/call",
        "https://agentpay.tools/tools/verified_route/call"))
    assert r["settled"] is True and r["indexed"] is False
    assert r["tx"] == "0xdeadbeef"
    assert s.calls == [("session_create", ka.KEEPALIVE_PARAMS)]
    assert any("MISSING" in m for m in out)


@pytest.mark.parametrize("exc", [
    TimeoutError("bazaar slow"),
    ConnectionError("dns"),
    ValueError("not json"),
])
def test_unreachable_index_never_buys(exc):
    """FAIL CLOSED ON SPENDING. An index we cannot read is not evidence that we
    are missing from it — paying on that would burn USDC on every outage."""
    s = _Session()
    out, log = _logs()

    def boom(_q):
        raise exc

    r = ka.keepalive(s, log, search=boom)
    assert r["settled"] is False and r["indexed"] is None
    assert s.calls == [], "spent money on an unreadable index"


@pytest.mark.parametrize("payload", [
    {},                       # resources key missing entirely
    {"resources": []},        # present but empty
    {"data": [{"x": 1}]},     # API renamed the key
    None,                     # body decoded to null
])
def test_empty_or_missing_resources_fails_closed(payload):
    """A 200 whose resources list is empty or missing is an API change or a
    degraded index, NOT absence — the brand query cannot legitimately be
    empty while eight rival "AgentPay" products exist. Paying here would
    settle $0.01 on every run until someone read the logs."""
    s = _Session()
    out, log = _logs()
    r = ka.keepalive(s, log, search=lambda q: payload)
    assert r["settled"] is False and r["indexed"] is None
    assert "implausible" in r["reason"]
    assert s.calls == [], "spent money on an implausible payload"


def test_budget_cap_wins_over_the_listing():
    s = _Session(over_cap=True)
    out, log = _logs()
    r = ka.keepalive(s, log, search=lambda q: _payload(
        "https://rival.example/their-agentpay-clone"))
    assert r["settled"] is False
    assert r["reason"] == "budget cap reached"
    assert s.calls == []


def test_settle_failure_degrades_and_never_raises():
    """Same lesson as 2026-08-07 (efa6a7e): a paid call that blows up must not
    take the run with it."""
    s = _Session(exc=RuntimeError("facilitator 502"))
    out, log = _logs()
    r = ka.keepalive(s, log, search=lambda q: _payload(
        "https://rival.example/their-agentpay-clone"))
    assert r["settled"] is False
    assert "settle failed" in r["reason"]


def test_disabled_switch_does_nothing_at_all():
    s = _Session()
    out, log = _logs()

    def must_not_run(_q):
        raise AssertionError("searched Bazaar while disabled")

    r = ka.keepalive(s, log, search=must_not_run, enabled=False)
    assert r == {"ran": False, "reason": "disabled"}
    assert s.calls == []


def test_probe_query_is_the_brand_term_on_purpose():
    """A head term can miss us for RANKING reasons; the brand term returns a
    small set, so absence there is real absence. Changing this to a head term
    would make the keepalive buy a settle it doesn't need."""
    assert ka.PROBE_QUERY == "agentpay"
