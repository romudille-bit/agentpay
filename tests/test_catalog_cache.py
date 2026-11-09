"""The in-memory Bazaar catalog behind verified_route (services/catalog_cache.py).

Pins: the whole catalog is paged in (not one search head); the snapshot is
lean; a cold or stale cache reads as None so callers sweep instead; a pull
that loses too many pages never replaces a good snapshot."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from gateway.services import catalog_cache as cc

URL = cc.RESOURCES_URL


def _item(i, payers=5, calls=20, desc="x"):
    return {
        "resource": f"https://seller{i}.x/api/tool{i}",
        "serviceName": f"Tool {i}",
        "description": desc,
        "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "10000",
                     "payTo": "0x" + f"{i:040x}", "outputSchema": {"type": "object"}}],
        "extensions": {"bazaar": {"info": {"input": {"method": "GET"}}}},
        "quality": {"l30DaysUniquePayers": payers, "l30DaysTotalCalls": calls},
        "tags": [],
    }


def _page(items, total, offset):
    return {"items": items, "pagination": {"limit": cc.PAGE, "offset": offset, "total": total}}


@pytest.fixture(autouse=True)
def _reset():
    cc._state.update(cands=None, fetched_at=None, total=0, pages_failed=0)
    yield
    cc._state.update(cands=None, fetched_at=None, total=0, pages_failed=0)


@respx.mock
async def test_pages_the_whole_catalog_and_trims():
    total = cc.PAGE + 7
    long = "d" * (cc.DESCRIPTION_CHARS + 50)
    respx.get(URL, params__contains={"offset": "0"}).mock(
        return_value=httpx.Response(200, json=_page([_item(i, desc=long) for i in range(cc.PAGE)], total, 0)))
    respx.get(URL, params__contains={"offset": str(cc.PAGE)}).mock(
        return_value=httpx.Response(200, json=_page([_item(cc.PAGE + i) for i in range(7)], total, cc.PAGE)))
    assert cc.get() is None                                   # cold
    n = await cc.refresh()
    assert n == total
    cands = cc.get()
    assert len(cands) == total
    assert len(cands[0]["description"]) == cc.DESCRIPTION_CHARS
    assert cands[0]["accepts_all"] == [cands[0]["accepts"]]  # single rail kept once
    assert cands[0]["payers30d"] == 5 and cands[0]["pay_to"].startswith("0x")
    st = cc.status()
    assert st["fresh"] and st["listings"] == total and st["reported_total"] == total


@respx.mock
async def test_stale_snapshot_reads_as_cold():
    respx.get(URL).mock(return_value=httpx.Response(200, json=_page([_item(1)], 1, 0)))
    await cc.refresh()
    assert cc.get() is not None
    cc._state["fetched_at"] = datetime.now(timezone.utc) - timedelta(hours=cc.MAX_AGE_H + 1)
    assert cc.get() is None
    assert cc.status()["fresh"] is False


@respx.mock
async def test_lossy_pull_keeps_the_previous_snapshot():
    good = [_item(i) for i in range(3)]
    first = respx.get(URL, params__contains={"offset": "0"}).mock(
        return_value=httpx.Response(200, json=_page(good, 3, 0)))
    await cc.refresh()
    assert len(cc.get()) == 3
    # second pull: first page fine, every other page 500s → more than 10% lost
    total = cc.PAGE * 5
    first.mock(return_value=httpx.Response(200, json=_page([_item(0)], total, 0)))
    for off in range(cc.PAGE, total, cc.PAGE):
        respx.get(URL, params__contains={"offset": str(off)}).mock(return_value=httpx.Response(500))
    cc.CONCURRENCY, old = 2, cc.CONCURRENCY
    try:
        n = await cc.refresh()
    finally:
        cc.CONCURRENCY = old
    assert n == 0
    assert len(cc.get()) == 3                                  # old snapshot survived


@respx.mock
async def test_empty_pull_keeps_the_previous_snapshot():
    respx.get(URL).mock(return_value=httpx.Response(200, json=_page([_item(1)], 1, 0)))
    await cc.refresh()
    respx.get(URL).mock(return_value=httpx.Response(200, json=_page([], 0, 0)))
    assert await cc.refresh() == 0
    assert len(cc.get()) == 1
