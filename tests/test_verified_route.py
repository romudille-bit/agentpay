"""
tests/test_verified_route.py — pure-function tests for the paid trust oracle.

No network: a synthetic Bazaar payload (plus the captured live fixture, if
present) is fed through radar.verified_route_from_payloads. Asserts the
sybil-collapse, survivor count, and ready-to-pay recommendation shape.
"""

import json
import pathlib
from decimal import Decimal

from gateway import radar


def _res(name, url, pay_to, amount="1000", payers=10, calls=50, schema=True,
         network="eip155:8453"):
    """One Bazaar resource record in discovery-payload shape."""
    return {
        "serviceName": name,
        "resource": {"url": url},
        "accepts": [{
            "scheme": "exact", "network": network, "amount": amount,
            "payTo": pay_to, "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "outputSchema": {"type": "object"} if schema else {},
        }],
        "quality": {"l30DaysUniquePayers": payers, "l30DaysTotalCalls": calls},
        "tags": [],
    }


# A factory wallet stamping 5 fake-distinct listings (≥ SYBIL_COLLAPSE_MIN) + 2
# independent real tools. (A 3–4 listing cluster would NOT collapse by design.)
FACTORY_WALLET = "0xfac0000000000000000000000000000000000000"
SYNTHETIC = {"resources": [
    _res("Fake A", "https://f.x/a", FACTORY_WALLET, payers=1, calls=2),
    _res("Fake B", "https://f.x/b", FACTORY_WALLET, payers=1, calls=1),
    _res("Fake C", "https://f.x/c", FACTORY_WALLET, payers=0, calls=0),
    _res("Fake D", "https://f.x/d", FACTORY_WALLET, payers=2, calls=3),
    _res("Fake E", "https://f.x/e", FACTORY_WALLET, payers=1, calls=4),
    _res("Real Otto", "https://otto.x/dex", "0x0e84ddedaae6a7000000000000000000000000",
         amount="1000", payers=200, calls=3246),
    _res("Real Exa", "https://exa.x/search", "0x52e29e0d2aa49b000000000000000000000000",
         amount="7000", payers=96, calls=1986),
]}


def test_collapses_factory_to_single_entry():
    out = radar.verified_route_from_payloads([SYNTHETIC], "data", Decimal("1"))
    cat = out["catalog"]
    # 7 listings scanned; the 5-listing factory collapses to 1 → 3 real providers.
    assert cat["scanned"] == 7
    assert cat["real_providers"] == 3
    assert cat["sybil_collapsed"] == 4            # 5 listings - 1 representative
    assert cat["biggest_factory"]["pay_to"] == FACTORY_WALLET
    assert cat["biggest_factory"]["listings"] == 5


def test_legit_multiproduct_provider_not_collapsed_by_usage():
    """A wallet with many PROVEN endpoints (real payers) keeps ALL of them and is
    not flagged a factory — decide on usage, not endpoint count (the CMC case)."""
    legit = {"resources": [
        _res(f"Multi {i}", f"https://m.x/{i}", "0xmulti0000000000000000000000000000000bbbb",
             payers=50 + i, calls=500 + i) for i in range(6)
    ]}
    out = radar.verified_route_from_payloads([legit], "data", Decimal("1"))
    assert out["catalog"]["sybil_collapsed"] == 0
    assert out["catalog"]["real_providers"] == 6
    assert out["catalog"]["biggest_factory"] is None
    assert all("factory" not in s["flags"] for s in out["survivors"])


def test_trusted_provider_never_collapsed_even_if_unproven():
    """A known-trusted wallet (CMC) keeps every endpoint even with an unproven
    tail — brand allowlist overrides the usage gate for reputable providers."""
    cmc_payto = next(iter(radar.KNOWN_TRUSTED))
    cmc = {"resources": [
        _res(f"CMC {i}", f"https://pro-api.coinmarketcap.com/x402/{i}", cmc_payto,
             payers=1, calls=1) for i in range(6)   # brand-new, unproven, but trusted
    ]}
    out = radar.verified_route_from_payloads([cmc], "crypto data", Decimal("1"))
    assert out["catalog"]["sybil_collapsed"] == 0
    assert out["catalog"]["real_providers"] == 6
    assert all("factory" not in s["flags"] for s in out["survivors"])


def test_small_cluster_not_collapsed():
    """A 4-listing wallet (below the ≥5 threshold) must stay fully visible."""
    four = {"resources": [
        _res("Quad 1", "https://q.x/1", "0xquad00000000000000000000000000000000aaaa", payers=5, calls=20),
        _res("Quad 2", "https://q.x/2", "0xquad00000000000000000000000000000000aaaa", payers=4, calls=15),
        _res("Quad 3", "https://q.x/3", "0xquad00000000000000000000000000000000aaaa", payers=3, calls=10),
        _res("Quad 4", "https://q.x/4", "0xquad00000000000000000000000000000000aaaa", payers=2, calls=8),
    ]}
    out = radar.verified_route_from_payloads([four], "data", Decimal("1"))
    assert out["catalog"]["sybil_collapsed"] == 0
    assert out["catalog"]["real_providers"] == 4
    assert out["catalog"]["biggest_factory"] is None


def test_recommendation_is_most_used_and_payable():
    out = radar.verified_route_from_payloads([SYNTHETIC], "data", Decimal("1"))
    rec = out["recommendation"]
    assert rec is not None
    assert rec["name"] == "Real Otto"            # highest unique-payer usage
    assert rec["ready_to_pay"]["network"] == "eip155:8453"
    assert rec["ready_to_pay"]["url"] == "https://otto.x/dex"
    assert rec["ready_to_pay"]["accepts"]["scheme"] == "exact"


def test_budget_gate_excludes_too_expensive():
    # Exa is $0.007; a $0.005 budget must drop it, leaving Otto ($0.001).
    out = radar.verified_route_from_payloads([SYNTHETIC], "data", Decimal("0.005"))
    names = {s["name"] for s in out["survivors"]}
    assert "Real Otto" in names
    assert "Real Exa" not in names


def test_factory_representative_carries_collapsed_count():
    out = radar.verified_route_from_payloads([SYNTHETIC], "data", Decimal("1"))
    fac = next((s for s in out["survivors"] if s["pay_to"] == FACTORY_WALLET), None)
    assert fac is not None
    assert fac["collapsed_siblings"] == 4


def test_merge_resources_dedups_by_url():
    merged = radar.merge_resources([SYNTHETIC, SYNTHETIC])   # same payload twice
    cands = radar.parse_resources(merged)
    assert len({c["url"] for c in cands}) == len(cands)      # no dup urls
    assert len(cands) == 7


def test_against_live_fixture_if_present():
    """If the captured live sweep exists, the real catalog must collapse hard."""
    fx = pathlib.Path(__file__).resolve().parents[1] / "tests/fixtures/bazaar_sweep.json"
    if not fx.exists():
        return
    data = json.loads(fx.read_text())
    out = radar.verified_route_from_payloads([data], "dex liquidity", Decimal("1"))
    cat = out["catalog"]
    assert cat["scanned"] > 50
    assert cat["real_providers"] < cat["scanned"]            # collapse actually happened
    assert cat["sybil_collapsed"] > 0
    assert out["recommendation"] is not None
