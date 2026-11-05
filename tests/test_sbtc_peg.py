"""
test_sbtc_peg.py — the sbtc_peg paid tool.

Hiro (blocks, call-read) and Emily (deposit/withdrawal listings, limits) are
replayed through httpx.MockTransport, so window cutting, the Clarity decoder,
pagination and the degrade paths are checked without network.
"""
import json

import httpx
import pytest

from gateway.services import sbtc_peg as sp

NOW_H, NOW_T = 9_418_770, 1_793_884_566          # Stacks height / unix time "now"
RATE = 7_000 / sp.DAY                             # blocks per second in the fixture chain
CUT_1D = int(NOW_H - RATE * sp.DAY)
CUT_7D = int(NOW_H - RATE * 7 * sp.DAY)


def _uint(n: int) -> str:
    return "01" + n.to_bytes(16, "big").hex()


def _principal_std(version=0x16, h=b"\x01" * 20) -> str:
    return "05" + bytes([version]).hex() + h.hex()


def _signer_data(n_signers=11, threshold=8) -> str:
    """(tuple (current-aggregate-pubkey (buff 33)) (current-signature-threshold uint)
    (current-signer-principal principal) (current-signer-set (list principal)))"""
    def name(s):
        return bytes([len(s)]).hex() + s.encode().hex()
    pub = "02" + (33).to_bytes(4, "big").hex() + "02" + "ab" * 32
    sset = "0b" + n_signers.to_bytes(4, "big").hex() + "".join(_principal_std(h=bytes([i]) * 20) for i in range(n_signers))
    body = (name("current-aggregate-pubkey") + pub + name("current-signature-threshold") + _uint(threshold)
            + name("current-signer-principal") + _principal_std() + name("current-signer-set") + sset)
    return "0x0c" + (4).to_bytes(4, "big").hex() + body


class FakeChain:
    def __init__(self, deposits, withdrawals, pending=(), accepted=(), failed_dep=(),
                 down=(), page_size=1000, supply=248_402_294_199):
        self.dep, self.wd = deposits, withdrawals
        self.pending, self.accepted, self.failed_dep = list(pending), list(accepted), list(failed_dep)
        self.down, self.page_size, self.supply = set(down), page_size, supply
        self.calls = []

    def _page(self, rows, params):
        start = int(params.get("nextToken") or 0)
        page = rows[start:start + self.page_size]
        nxt = str(start + self.page_size) if start + self.page_size < len(rows) else None
        return page, nxt

    def handler(self, req: httpx.Request) -> httpx.Response:
        h, p, q = req.url.host, req.url.path, dict(req.url.params)
        self.calls.append((p, q.get("status"), q.get("nextToken")))
        if h == "api.hiro.so":
            if p == "/extended/v2/blocks":
                return httpx.Response(200, json={"results": [{"height": NOW_H, "burn_block_height": 974_675, "block_time": NOW_T}]})
            if p.startswith("/extended/v2/blocks/"):
                hh = int(p.rsplit("/", 1)[1])
                return httpx.Response(200, json={"height": hh, "burn_block_height": 974_000,
                                                 "block_time": int(NOW_T - (NOW_H - hh) / RATE)})
            if p.endswith("/get-total-supply"):
                if "supply" in self.down:
                    return httpx.Response(502, text="bad gateway")
                return httpx.Response(200, json={"okay": True, "result": "0x07" + _uint(self.supply)})
            if p.endswith("/get-current-signer-data"):
                if "signer" in self.down:
                    return httpx.Response(200, json={"okay": False, "cause": "boom"})
                return httpx.Response(200, json={"okay": True, "result": _signer_data()})
        if h == "sbtc-emily.com":
            if "emily" in self.down:
                return httpx.Response(503, text="down")
            if p == "/limits":
                return httpx.Response(200, json={"pegCap": 2_100_000_000_000_000, "perDepositMinimum": 10_000,
                                                 "perWithdrawalCap": 5_100_000_000, "rollingWithdrawalBlocks": 150,
                                                 "rollingWithdrawalCap": 15_100_000_000,
                                                 "availableToWithdraw": 14_786_411_900, "throttleModeInitiator": None})
            status = q.get("status")
            if p == "/deposit":
                rows = {"confirmed": self.dep, "pending": self.pending, "accepted": self.accepted,
                        "failed": self.failed_dep}.get(status, [])
                page, nxt = self._page(rows, q)
                return httpx.Response(200, json={"deposits": page, "nextToken": nxt})
            if p == "/withdrawal":
                rows = self.wd if status == "confirmed" else []
                page, nxt = self._page(rows, q)
                return httpx.Response(200, json={"withdrawals": page, "nextToken": nxt})
        return httpx.Response(404)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def dep(height, sats, recipient="0616000000000000000000000000000000000000000005706f782d35", txid="aa" * 32):
    return {"lastUpdateHeight": height, "amount": sats, "recipient": recipient, "bitcoinTxid": txid, "status": "confirmed"}


def wd(height, sats, rid=1):
    return {"lastUpdateHeight": height, "amount": sats, "sender": "SP1", "txid": "bb" * 32,
            "requestId": rid, "status": "confirmed"}


# newest first, as Emily serves them
DEPOSITS = [dep(NOW_H - 10, 85_000), dep(NOW_H - 500, 40_000, recipient="051a" + "cc" * 20),
            dep(CUT_1D - 5, 1_000_000),                 # just outside 24h, inside 7d
            dep(CUT_7D - 5, 99)]                        # outside 7d
WITHDRAWALS = [wd(NOW_H - 20, 4_288_138, 4830), wd(CUT_1D - 100, 1_000_000, 4829), wd(CUT_7D - 1, 5, 1)]


def test_clarity_decoder_roundtrips_the_registry_tuple():
    v = sp.decode_clarity(_signer_data(n_signers=3, threshold=2))
    assert v["current-signature-threshold"] == 2
    assert len(v["current-signer-set"]) == 3
    assert v["current-aggregate-pubkey"].startswith("0x02")
    assert sp.decode_clarity("0x07" + _uint(42)) == {"ok": 42}
    assert sp.decode_clarity("0x09") is None and sp.decode_clarity("0x03") is True


def test_principal_str():
    assert sp.principal_str("0616000000000000000000000000000000000000000005706f782d35") == "SP000000000000000000002Q6VF78.pox-5"
    assert sp.principal_str("051a" + "ab" * 20).startswith("ST")
    assert sp.principal_str("zz") == "zz" and sp.principal_str(None) is None


@pytest.mark.asyncio
async def test_windows_cut_by_height():
    chain = FakeChain(DEPOSITS, WITHDRAWALS)
    async with chain.client() as c:
        r = await sp.fetch(c, {})
    d, w = r["flows"]["24h"], r["flows"]["7d"]
    assert d["peg_in_count"] == 2 and d["peg_in_sats"] == 125_000 and d["peg_in_unique_recipients"] == 2
    assert d["peg_out_count"] == 1 and d["peg_out_sats"] == 4_288_138
    assert d["net_sats"] == 125_000 - 4_288_138
    assert w["peg_in_count"] == 3 and w["peg_in_sats"] == 1_125_000
    assert w["peg_out_count"] == 2 and w["complete"] is True
    assert r["supply"]["btc"] == pytest.approx(2484.02294199)
    assert r["supply"]["pct_of_cap"] == pytest.approx(0.0118, abs=1e-4)
    assert r["as_of"]["stacks_height"] == NOW_H and r["notes"] == []


@pytest.mark.asyncio
async def test_signers_limits_and_last_events():
    chain = FakeChain(DEPOSITS, WITHDRAWALS, pending=[dep(NOW_H - 14_000, 50_000)], accepted=[dep(NOW_H, 1)])
    async with chain.client() as c:
        r = await sp.fetch(c, {})
    s = r["signers"]
    assert s["count"] == 11 and s["signature_threshold"] == 8 and s["liveness"] is None
    assert s["limits"]["withdrawal_headroom_pct"] == pytest.approx(97.9, abs=0.1)
    assert r["in_flight"]["deposits_pending"] == 1 and r["in_flight"]["deposits_accepted"] == 1
    assert r["in_flight"]["oldest_pending_deposit_age_hours"] == pytest.approx(14_000 / RATE / 3600, abs=0.2)
    ev = r["last_events"]
    assert ev[0] == {"type": "peg_in", "sats": 85_000, "stacks_height": NOW_H - 10, "bitcoin_txid": "aa" * 32,
                     "recipient": "SP000000000000000000002Q6VF78.pox-5"}
    assert ev[1]["type"] == "peg_out" and ev[1]["request_id"] == 4830
    assert all(ev[i]["stacks_height"] >= ev[i + 1]["stacks_height"] for i in range(len(ev) - 1))
    assert "8-of-11 signers" in r["read"] and "headroom 97.9%" in r["read"]


@pytest.mark.asyncio
async def test_pagination_stops_at_the_window_edge():
    many = [dep(NOW_H - i * 10, 1_000) for i in range(2_500)] + [dep(CUT_7D - 1, 7)]
    chain = FakeChain(many, [], page_size=1000)
    async with chain.client() as c:
        r = await sp.fetch(c, {})
    assert r["flows"]["7d"]["peg_in_count"] == 2_500 and r["flows"]["7d"]["complete"] is True
    confirmed_pages = [x for x in chain.calls if x[0] == "/deposit" and x[1] == "confirmed"]
    assert len(confirmed_pages) == 3


@pytest.mark.asyncio
async def test_pagination_cap_marks_the_window_as_a_floor():
    many = [dep(NOW_H - i, 1) for i in range(sp.MAX_PAGES * 50 + 10)]
    chain = FakeChain(many, [], page_size=50)
    async with chain.client() as c:
        r = await sp.fetch(c, {})
    assert r["flows"]["7d"]["complete"] is False
    assert r["flows"]["7d"]["peg_in_count"] == sp.MAX_PAGES * 50
    assert any("truncated" in n for n in r["notes"]) and "floor" in r["read"]


@pytest.mark.asyncio
async def test_sources_degrade_to_null_with_a_note():
    chain = FakeChain(DEPOSITS, WITHDRAWALS, down={"supply", "signer"})
    async with chain.client() as c:
        r = await sp.fetch(c, {})
    assert r["supply"] is None
    assert "count" not in r["signers"] and "limits" in r["signers"]
    assert any(n.startswith("supply unavailable") for n in r["notes"])
    assert any(n.startswith("signer data unavailable") for n in r["notes"])
    assert r["flows"]["24h"]["peg_in_count"] == 2            # Emily still answered


@pytest.mark.asyncio
async def test_emily_down_keeps_supply_and_signers():
    chain = FakeChain(DEPOSITS, WITHDRAWALS, down={"emily"})
    async with chain.client() as c:
        r = await sp.fetch(c, {})
    assert r["flows"] is None and r["last_events"] == []
    assert r["in_flight"]["deposits_pending"] is None
    assert r["supply"]["sats"] == 248_402_294_199 and r["signers"]["count"] == 11
    assert "limits" not in r["signers"]
    assert r["read"].startswith("sBTC supply 2484.02 BTC; 8-of-11 signers")


@pytest.mark.asyncio
async def test_dispatcher_routes_and_caches(monkeypatch):
    from gateway.services import tools_runtime, cache as cache_mod
    calls = {"n": 0}

    async def fake_fetch(client, params):
        calls["n"] += 1
        return {"asset": "sBTC", "supply": {"btc": 1.0}}

    monkeypatch.setattr(sp, "fetch", fake_fetch)
    cache_mod._cache.clear()
    r1 = await tools_runtime.real_tool_response("sbtc_peg", {})
    r2 = await tools_runtime.real_tool_response("sbtc_peg", {})
    assert r1["supply"]["btc"] == 1.0 and r2.get("cached") is True and calls["n"] == 1


def test_registry_entry_is_paid_and_discoverable():
    import registry
    from gateway.routes import tools as rt
    t = registry.get_tool("sbtc_peg")
    assert t and t.price_usdc == "0.01" and t.parameters["required"] == []
    assert "sbtc_peg" in rt._TOOL_BAZAAR and "sbtc_peg" in rt._PAID_RELATED
