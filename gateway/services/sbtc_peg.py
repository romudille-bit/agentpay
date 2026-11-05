"""sbtc_peg — the sBTC peg as an agent-readable feed (mainnet).

Supply from the token contract, peg-in/peg-out flows and in-flight requests
from Emily (the sBTC bridge API the signers run), signer set and limits from
the registry contract. Windows are cut by Stacks height; the height↔time
edge is interpolated from block timestamps, so a window is accurate to a few
minutes. Everything is public and keyless; a source that fails degrades to
`null` plus a note, never a 500. Field meanings in docs/sbtc-peg.md.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from agentpay._stacks_tx import c32_address

HIRO = "https://api.hiro.so"
EMILY = "https://sbtc-emily.com"
DEPLOYER = "SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4"
TOKEN = f"{DEPLOYER}.sbtc-token"
REGISTRY = f"{DEPLOYER}.sbtc-registry"
DAY = 86_400
MAX_PAGES = 12                      # Emily pages per listing — bounds a cold call
LAST_EVENTS = 10


# ── Clarity value decoding (only the types the registry returns) ─────────────

def decode_clarity(hexstr: str) -> Any:
    """Decode a serialized Clarity value: int/uint, bool, buff, principal,
    optional, response, list, tuple, string. Unknown prefixes raise."""
    data = bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)
    val, _ = _cv(data, 0)
    return val


def _cv(b: bytes, i: int):
    t = b[i]; i += 1
    if t == 0x00:
        return int.from_bytes(b[i:i + 16], "big", signed=True), i + 16
    if t == 0x01:
        return int.from_bytes(b[i:i + 16], "big"), i + 16
    if t == 0x02:
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        return "0x" + b[i:i + n].hex(), i + n
    if t == 0x03:
        return True, i
    if t == 0x04:
        return False, i
    if t == 0x05:
        return {"principal": "0x" + b[i:i + 21].hex()}, i + 21
    if t == 0x06:
        h = "0x" + b[i:i + 21].hex(); i += 21
        n = b[i]; i += 1
        return {"principal": h, "contract": b[i:i + n].decode()}, i + n
    if t in (0x07, 0x08):
        v, i = _cv(b, i)
        return {"ok" if t == 0x07 else "err": v}, i
    if t == 0x09:
        return None, i
    if t == 0x0a:
        return _cv(b, i)
    if t == 0x0b:
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        out = []
        for _ in range(n):
            v, i = _cv(b, i); out.append(v)
        return out, i
    if t == 0x0c:
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        out = {}
        for _ in range(n):
            k = b[i]; i += 1
            name = b[i:i + k].decode(); i += k
            out[name], i = _cv(b, i)
        return out, i
    if t in (0x0d, 0x0e):
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        return b[i:i + n].decode("utf-8", "replace"), i + n
    raise ValueError(f"clarity type 0x{t:02x}")


def principal_str(hexstr: Optional[str]) -> Optional[str]:
    """Emily's hex-serialized recipient principal → SP… / SP….name; unknown shapes pass through."""
    if not hexstr:
        return hexstr
    try:
        b = bytes.fromhex(hexstr)
        if b[0] == 0x05 and len(b) == 22:
            return c32_address(b[1], b[2:22])
        if b[0] == 0x06 and len(b) >= 23:
            return c32_address(b[1], b[2:22]) + "." + b[23:23 + b[22]].decode()
    except Exception:
        pass
    return hexstr


# ── Sources ──────────────────────────────────────────────────────────────────

async def _call_read(c: httpx.AsyncClient, contract: str, fn: str) -> Any:
    addr, name = contract.split(".")
    r = await c.post(f"{HIRO}/v2/contracts/call-read/{addr}/{name}/{fn}",
                     json={"sender": addr, "arguments": []})
    r.raise_for_status()
    d = r.json()
    if not d.get("okay"):
        raise ValueError(d.get("cause") or "call-read failed")
    return decode_clarity(d["result"])


async def _block(c: httpx.AsyncClient, height: Optional[int] = None) -> dict:
    url = f"{HIRO}/extended/v2/blocks/{height}" if height else f"{HIRO}/extended/v2/blocks?limit=1"
    r = await c.get(url)
    r.raise_for_status()
    d = r.json()
    b = d["results"][0] if height is None else d
    return {"height": int(b["height"]), "burn_height": int(b["burn_block_height"]),
            "time": int(b["block_time"])}


async def _emily_list(c: httpx.AsyncClient, kind: str, status: str, min_height: int,
                      max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    """Walk a listing newest-first until lastUpdateHeight < min_height.
    Returns (rows at/after min_height, complete)."""
    rows: list[dict] = []
    token = None
    for _ in range(max_pages):
        params = {"status": status, "pageSize": 1000}
        if token:
            params["nextToken"] = token
        r = await c.get(f"{EMILY}/{kind}", params=params)
        r.raise_for_status()
        d = r.json()
        page = d.get(f"{kind}s") or []
        for row in page:
            if int(row.get("lastUpdateHeight") or 0) < min_height:
                return rows, True
            rows.append(row)
        token = d.get("nextToken")
        if not token or not page:
            return rows, True
    return rows, False


async def _limits(c: httpx.AsyncClient) -> dict:
    r = await c.get(f"{EMILY}/limits")
    r.raise_for_status()
    return r.json()


# ── Assembly ─────────────────────────────────────────────────────────────────

def _window(rows_dep: list[dict], rows_wd: list[dict], min_height: int, complete: bool) -> dict:
    dep = [r for r in rows_dep if int(r["lastUpdateHeight"]) >= min_height]
    wd = [r for r in rows_wd if int(r["lastUpdateHeight"]) >= min_height]
    pin = sum(int(r["amount"]) for r in dep)
    pout = sum(int(r["amount"]) for r in wd)
    return {
        "peg_in_sats": pin, "peg_in_count": len(dep),
        "peg_in_unique_recipients": len({r.get("recipient") for r in dep}),
        "peg_out_sats": pout, "peg_out_count": len(wd),
        "net_sats": pin - pout,
        "from_stacks_height": min_height,
        "complete": complete,
    }


def _age_hours(height: int, now: dict, rate: float) -> float:
    return round(max(0, now["height"] - height) / rate / 3600, 1)


async def fetch(client: httpx.AsyncClient, params: dict) -> dict:
    notes: list[str] = []
    now = await _block(client)
    sample_1d, sample_7d = await asyncio.gather(
        _block(client, now["height"] - 7_000), _block(client, now["height"] - 50_000),
        return_exceptions=True)
    # blocks per second over each sample span; the 7d edge uses the longer span
    if isinstance(sample_1d, dict) and now["time"] > sample_1d["time"]:
        rate_1d = (now["height"] - sample_1d["height"]) / (now["time"] - sample_1d["time"])
    else:
        rate_1d = 7_000 / DAY
        notes.append("24h window edge estimated from an assumed block rate (block lookup failed)")
    if isinstance(sample_7d, dict) and now["time"] > sample_7d["time"]:
        rate_7d = (now["height"] - sample_7d["height"]) / (now["time"] - sample_7d["time"])
    else:
        rate_7d = rate_1d
    h_1d = int(now["height"] - rate_1d * DAY)
    h_7d = int(now["height"] - rate_7d * 7 * DAY)

    async def guarded(coro, label):
        try:
            return await coro
        except Exception as e:
            notes.append(f"{label} unavailable: {_why(e)}")
            return None

    supply, signer, limits, dep7, wd7, dep_pending, dep_accepted, wd_pending, wd_accepted, dep_failed, wd_failed = \
        await asyncio.gather(
            guarded(_call_read(client, TOKEN, "get-total-supply"), "supply"),
            guarded(_call_read(client, REGISTRY, "get-current-signer-data"), "signer data"),
            guarded(_limits(client), "limits"),
            guarded(_emily_list(client, "deposit", "confirmed", h_7d), "confirmed deposits"),
            guarded(_emily_list(client, "withdrawal", "confirmed", h_7d), "confirmed withdrawals"),
            guarded(_emily_list(client, "deposit", "pending", 0, max_pages=1), "pending deposits"),
            guarded(_emily_list(client, "deposit", "accepted", 0, max_pages=1), "accepted deposits"),
            guarded(_emily_list(client, "withdrawal", "pending", 0, max_pages=1), "pending withdrawals"),
            guarded(_emily_list(client, "withdrawal", "accepted", 0, max_pages=1), "accepted withdrawals"),
            guarded(_emily_list(client, "deposit", "failed", h_1d, max_pages=1), "failed deposits"),
            guarded(_emily_list(client, "withdrawal", "failed", h_1d, max_pages=1), "failed withdrawals"),
        )

    supply_sats = supply.get("ok") if isinstance(supply, dict) else None
    dep_rows, dep_complete = dep7 if dep7 else ([], False)
    wd_rows, wd_complete = wd7 if wd7 else ([], False)
    flows = None
    if dep7 or wd7:
        flows = {
            "24h": _window(dep_rows, wd_rows, h_1d, True),
            "7d": _window(dep_rows, wd_rows, h_7d, dep_complete and wd_complete),
        }
        if not (dep_complete and wd_complete):
            notes.append(f"7d flows truncated at {MAX_PAGES} pages — counts are a floor")

    def _count(x):
        return len(x[0]) if x else None

    pend = dep_pending[0] if dep_pending else []
    in_flight = {
        "deposits_pending": _count(dep_pending),
        "deposits_accepted": _count(dep_accepted),
        "oldest_pending_deposit_age_hours": (
            _age_hours(min(int(r["lastUpdateHeight"]) for r in pend), now, rate_1d) if pend else None),
        "withdrawals_pending": _count(wd_pending),
        "withdrawals_accepted": _count(wd_accepted),
        "deposits_failed_24h": _count(dep_failed),
        "withdrawals_failed_24h": _count(wd_failed),
    }

    events = sorted(
        [{"type": "peg_in", "sats": int(r["amount"]), "stacks_height": int(r["lastUpdateHeight"]),
          "bitcoin_txid": r.get("bitcoinTxid"), "recipient": principal_str(r.get("recipient"))} for r in dep_rows] +
        [{"type": "peg_out", "sats": int(r["amount"]), "stacks_height": int(r["lastUpdateHeight"]),
          "bitcoin_txid": r.get("txid"), "sender": r.get("sender"), "request_id": r.get("requestId")}
         for r in wd_rows],
        key=lambda e: e["stacks_height"], reverse=True)[:LAST_EVENTS]

    signers = None
    if isinstance(signer, dict):
        sset = signer.get("current-signer-set") or []
        signers = {
            "count": len(sset),
            "signature_threshold": signer.get("current-signature-threshold"),
            "aggregate_pubkey": signer.get("current-aggregate-pubkey"),
        }
    if isinstance(limits, dict):
        avail = limits.get("availableToWithdraw"); cap = limits.get("rollingWithdrawalCap")
        signers = {**(signers or {}), "limits": {
            "peg_cap_sats": limits.get("pegCap"),
            "per_deposit_minimum_sats": limits.get("perDepositMinimum"),
            "per_withdrawal_cap_sats": limits.get("perWithdrawalCap"),
            "rolling_withdrawal_cap_sats": cap,
            "rolling_window_blocks": limits.get("rollingWithdrawalBlocks"),
            "available_to_withdraw_sats": avail,
            "withdrawal_headroom_pct": round(100 * avail / cap, 1) if avail is not None and cap else None,
            "throttle_mode": limits.get("throttleModeInitiator"),
        }}
    if signers is not None:
        signers["liveness"] = None
        signers["note"] = ("signer liveness is not published; the observable proxies are "
                           "pending/accepted deposit ages and the rolling withdrawal headroom")

    peg_cap = (limits or {}).get("pegCap") if isinstance(limits, dict) else None
    supply_block = None
    if supply_sats is not None:
        supply_block = {"sats": supply_sats, "btc": round(supply_sats / 1e8, 8),
                        "peg_cap_sats": peg_cap,
                        "pct_of_cap": round(100 * supply_sats / peg_cap, 4) if peg_cap else None}

    read = _read(supply_block, flows, in_flight, signers)
    return {
        "asset": "sBTC", "contract": TOKEN, "network": "stacks-mainnet",
        "as_of": {"time": datetime.fromtimestamp(now["time"], tz=timezone.utc).isoformat(timespec="seconds"),
                  "stacks_height": now["height"], "bitcoin_height": now["burn_height"]},
        "supply": supply_block,
        "flows": flows,
        "in_flight": in_flight,
        "last_events": events,
        "signers": signers,
        "read": read,
        "sources": {"supply": f"{HIRO} call-read {TOKEN}.get-total-supply",
                    "flows": f"{EMILY}/deposit, /withdrawal (status=confirmed)",
                    "signers": f"{REGISTRY}.get-current-signer-data + {EMILY}/limits"},
        "notes": notes,
    }


def _read(supply, flows, in_flight, signers) -> str:
    parts = []
    if supply:
        parts.append(f"sBTC supply {supply['btc']:.2f} BTC")
    if flows:
        d = flows["24h"]; w = flows["7d"]
        parts.append(f"24h net {d['net_sats'] / 1e8:+.4f} BTC ({d['peg_in_count']} in / {d['peg_out_count']} out)")
        parts.append(f"7d net {w['net_sats'] / 1e8:+.4f} BTC ({w['peg_in_count']} in / {w['peg_out_count']} out)"
                     + ("" if w["complete"] else ", floor"))
    if in_flight.get("deposits_pending") is not None:
        age = in_flight.get("oldest_pending_deposit_age_hours")
        parts.append(f"{in_flight['deposits_pending']} deposits pending"
                     + (f" (oldest {age}h)" if age is not None else ""))
    if signers and signers.get("count"):
        parts.append(f"{signers['signature_threshold']}-of-{signers['count']} signers")
    lim = (signers or {}).get("limits") or {}
    if lim.get("withdrawal_headroom_pct") is not None:
        parts.append(f"withdrawal headroom {lim['withdrawal_headroom_pct']}%")
    return "; ".join(parts) if parts else "no source answered"


def _why(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"http {e.response.status_code}"
    if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    return (str(e) or type(e).__name__)[:80]
