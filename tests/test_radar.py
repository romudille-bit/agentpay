"""
tests/test_radar.py — pure-function tests for the Arbitrum x402 Radar core.

No network: a captured-style Bazaar discovery payload is fed through
parse_resources → filter_chain → decide / rank.
"""

import importlib.machinery
import importlib.util
import pathlib
from decimal import Decimal

from gateway import radar


def _load_plugin_router():
    """Load the standalone plugin router (no .py extension) as a module.

    It imports stdlib only and guards main with __name__, so executing it is safe.
    """
    p = pathlib.Path(__file__).resolve().parents[1] / "plugins/agentpay/bin/agentpay-route"
    loader = importlib.machinery.SourceFileLoader("agentpay_route", str(p))
    spec = importlib.util.spec_from_loader("agentpay_route", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_chain_map_in_sync_with_plugin_router():
    """The plugin router duplicates CHAIN_NETWORKS by design — guard against drift."""
    plugin = _load_plugin_router()
    assert plugin.CHAIN_NETWORKS == radar.CHAIN_NETWORKS


def _q(calls, payers, last="2026-06-06T00:00:00Z"):
    return {"l30DaysTotalCalls": calls, "l30DaysUniquePayers": payers, "lastCalledAt": last}


SAMPLE = {
    "resources": [
        {  # Arbitrum One — real, well used
            "serviceName": "arb-funding",
            "resource": {"url": "https://arb.example/funding"},
            "accepts": [{
                "amount": 2000, "network": "eip155:42161", "payTo": "0xAAA1",
                "outputSchema": {"type": "object"},
            }],
            "quality": _q(400, 50),
        },
        {  # Arbitrum Sepolia — real, low usage
            "serviceName": "arb-sepolia-tool",
            "resource": {"url": "https://arb.example/sep"},
            "accepts": [{
                "amount": 1000, "network": "eip155:421614", "payTo": "0xBBB1",
                "outputSchema": {"type": "object"},
            }],
            "quality": _q(10, 2),
        },
        {  # Base — should be filtered out for arbitrum-stack
            "serviceName": "base-tool",
            "resource": {"url": "https://base.example/x"},
            "accepts": [{
                "amount": 1000, "network": "eip155:8453", "payTo": "0xCCC1",
                "outputSchema": {"type": "object"},
            }],
            "quality": _q(9999, 9999),
        },
        {  # Arbitrum One stub — no schema → dropped by decide
            "serviceName": "arb-stub",
            "resource": {"url": "https://arb.example/stub"},
            "accepts": [{"amount": 500, "network": "eip155:42161", "payTo": "0xDDD1"}],
            "quality": _q(0, 0),
        },
    ]
}

# Factory: one payTo behind 3 distinct names, all on Arbitrum One, all with schema.
FACTORY = {
    "resources": [
        {
            "serviceName": f"factory-{i}",
            "resource": {"url": f"https://f.example/{i}"},
            "accepts": [{
                "amount": 1000, "network": "eip155:42161", "payTo": "0xFAC7",
                "outputSchema": {"type": "object"},
            }],
            "quality": _q(100, 10),
        }
        for i in range(3)
    ]
}


def test_networks_for_groups():
    assert radar.networks_for("arbitrum-stack") == {
        "eip155:42161", "eip155:421614", "eip155:46630"}
    assert radar.networks_for("arbitrum") == {"eip155:42161"}
    assert radar.networks_for("robinhood") == {"eip155:46630"}
    assert radar.networks_for(None) is None
    # unknown key passes through as a literal caip2
    assert radar.networks_for("eip155:999") == {"eip155:999"}


def test_normalize_aliases():
    assert radar.normalize_network("arbitrum-one") == "eip155:42161"
    assert radar.normalize_network("eip155:42161") == "eip155:42161"
    assert radar.normalize_network("Base-Mainnet") == "eip155:8453"


def test_filter_chain_keeps_only_stack():
    cands = radar.parse_resources(SAMPLE)
    assert len(cands) == 4
    stack = radar.filter_chain(cands, "arbitrum-stack")
    urls = {c["url"] for c in stack}
    assert "https://base.example/x" not in urls          # Base excluded
    assert "https://arb.example/funding" in urls
    assert "https://arb.example/sep" in urls
    assert len(stack) == 3


def test_filter_chain_none_returns_all():
    cands = radar.parse_resources(SAMPLE)
    assert len(radar.filter_chain(cands, None)) == 4


def test_decide_drops_stub_and_ranks_by_usage():
    cands = radar.filter_chain(radar.parse_resources(SAMPLE), "arbitrum-stack")
    scored, rec = radar.decide(cands, Decimal("0.01"))
    # stub (no schema) is dropped
    stub = next(s for s in scored if s["url"] == "https://arb.example/stub")
    assert stub["dropped"] and "stub" in stub["drop_reason"]
    # best-used Arbitrum tool wins
    assert rec is not None
    assert rec["url"] == "https://arb.example/funding"


def test_budget_gate():
    cands = radar.filter_chain(radar.parse_resources(SAMPLE), "arbitrum-stack")
    # budget below the 0.002 funding tool but above the 0.001 sepolia tool
    _, rec = radar.decide(cands, Decimal("0.0015"))
    assert rec is not None
    assert rec["url"] == "https://arb.example/sep"


def test_factory_downranked():
    cands = radar.parse_resources(FACTORY)
    scored, _ = radar.decide(cands, Decimal("0.01"))
    assert all("factory" in s["flags"] for s in scored)


def test_rank_end_to_end_with_injected_fetch():
    out = radar.rank("funding", Decimal("0.01"), chain="arbitrum-stack",
                     fetch=lambda url: SAMPLE)
    assert out["chain"] == "arbitrum-stack"
    assert out["recommendation"]["url"] == "https://arb.example/funding"
    # only stack survivors with schema + in budget are listed
    listed = {r["url"] for r in out["results"]}
    assert "https://base.example/x" not in listed
    assert "https://arb.example/stub" not in listed


def _candidate(url, network_caip2, name, calls=0, payers=0, price="0.001",
               schema=True, pay_to="0xRH1"):
    """A candidate in parse_resources shape (what the Robinhood crawler emits)."""
    return {
        "name": name, "url": url, "price_usd": Decimal(price),
        "network": network_caip2, "network_caip2": network_caip2,
        "pay_to": pay_to.lower(), "tags": [], "has_schema": schema,
        "calls30d": calls, "payers30d": payers, "last_called": "2026-06-06T00:00:00Z",
    }


def test_rank_from_payload_matches_rank():
    a = radar.rank_from_payload(SAMPLE, "funding", Decimal("0.01"), chain="arbitrum-stack")
    b = radar.rank("funding", Decimal("0.01"), chain="arbitrum-stack", fetch=lambda u: SAMPLE)
    assert a == b


def test_rank_from_payload_injects_robinhood_extra():
    # Robinhood candidate Bazaar can't see, injected via `extra`.
    rh = _candidate("https://rh.example/oracle", "eip155:46630", "rh-oracle",
                    calls=999, payers=99)
    out = radar.rank_from_payload(SAMPLE, "oracle", Decimal("0.01"),
                                  chain="arbitrum-stack", extra=[rh])
    listed = {r["url"] for r in out["results"]}
    assert "https://rh.example/oracle" in listed       # robinhood surfaced
    assert "https://base.example/x" not in listed      # base still excluded
    # highest usage → recommended
    assert out["recommendation"]["url"] == "https://rh.example/oracle"


def test_extra_respects_chain_filter():
    # A Base candidate injected as extra must still be filtered out for the stack.
    base_extra = _candidate("https://base.example/extra", "eip155:8453", "base-extra")
    out = radar.rank_from_payload(SAMPLE, "x", Decimal("0.01"),
                                  chain="arbitrum-stack", extra=[base_extra])
    assert "https://base.example/extra" not in {r["url"] for r in out["results"]}
