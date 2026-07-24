"""
test_testnet_paid_override.py — AGE-77.

The testnet M1 demo needs a nonzero-priced tool, but the registry price is
shared with mainnet (where the free-funnel keeps data tools at $0). So
TESTNET_PAID_TOOLS is an env-gated, testnet-only override applied at request
time. This locks in:
  * the override applies to a listed tool and returns a COPY (registry untouched)
  * env unset -> no change (mainnet stays free-funnel)
  * tools not in the list are untouched; None passes straight through
  * multi-pair parsing
"""

import pytest

import registry
from gateway.config import settings
from gateway.routes import tools as tools_route


@pytest.fixture
def restore_env():
    original = settings.TESTNET_PAID_TOOLS
    yield
    settings.TESTNET_PAID_TOOLS = original


def test_override_applies_and_copies(restore_env):
    settings.TESTNET_PAID_TOOLS = "token_price:0.01"
    base = registry.get_tool("token_price")
    assert base.price_usdc == "0.000"                 # free in the shared registry
    out = tools_route._apply_demo_pricing(base)
    assert out.price_usdc == "0.01"                    # testnet demo price
    assert out.name == "token_price"
    assert base.price_usdc == "0.000"                  # original untouched (copy)


def test_unset_is_noop(restore_env):
    settings.TESTNET_PAID_TOOLS = ""
    base = registry.get_tool("token_price")
    assert tools_route._apply_demo_pricing(base).price_usdc == base.price_usdc


def test_unlisted_tool_untouched(restore_env):
    settings.TESTNET_PAID_TOOLS = "token_price:0.01"
    # any other registered tool must keep its registry price
    other = next(t for t in registry.list_tools() if t.name != "token_price")
    assert tools_route._apply_demo_pricing(other).price_usdc == other.price_usdc


def test_none_passes_through(restore_env):
    settings.TESTNET_PAID_TOOLS = "token_price:0.01"
    assert tools_route._apply_demo_pricing(None) is None


def test_multi_pair_parse(restore_env):
    settings.TESTNET_PAID_TOOLS = "token_price:0.01, gas_tracker:0.02"
    assert tools_route._demo_price_overrides() == {
        "token_price": "0.01", "gas_tracker": "0.02"}


def test_malformed_pairs_ignored(restore_env):
    settings.TESTNET_PAID_TOOLS = "token_price:0.01,garbage,:0.02,noprice:"
    assert tools_route._demo_price_overrides() == {"token_price": "0.01"}
