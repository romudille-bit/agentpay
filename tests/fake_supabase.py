"""In-memory stand-in for gateway.services.supabase with the *enabled*
contract: insert-only replay tables (repeat → False, like a PK 409), an
outage switch that fails closed (None), and enough of payment_logs /
pending_challenges for the settle paths to run end to end.

Route tests otherwise run Supabase-disabled, where every record_* call
returns True — the blind spot that hid the stale-nonce recovery bug.
"""

from __future__ import annotations

import asyncio
from typing import Optional


class FakeSupabase:
    def __init__(self) -> None:
        self.payment_ids: set[str] = set()
        self.tx_hashes: set[tuple[str, str]] = set()
        self.challenges: dict[str, dict] = {}
        self.logs: dict[str, dict] = {}
        self.calls: list[tuple] = []
        # Replay-store writes answer None (durable consume unconfirmed):
        # all of them, or only the named ones.
        self.outage = False
        self.outage_names: set[str] = set()
        # Yield to the event loop before each insert so two concurrent
        # settles of the same proof interleave like real network I/O.
        self.yield_before_write = False

    async def _write_gate(self, name: str, *key) -> Optional[bool]:
        self.calls.append((name, *key))
        if self.yield_before_write:
            await asyncio.sleep(0)
        if self.outage or name in self.outage_names:
            return None
        return True

    # ── replay tables ────────────────────────────────────────────────────
    async def record_payment_id(self, payment_id: str) -> bool | None:
        gate = await self._write_gate("record_payment_id", payment_id)
        if gate is None:
            return None
        if payment_id in self.payment_ids:
            return False
        self.payment_ids.add(payment_id)
        return True

    async def record_tx_hash(self, tx_hash: str, network: str) -> bool | None:
        gate = await self._write_gate("record_tx_hash", tx_hash, network)
        if gate is None:
            return None
        if (tx_hash, network) in self.tx_hashes:
            return False
        self.tx_hashes.add((tx_hash, network))
        return True

    async def unrecord_tx_hash(self, tx_hash: str, network: str) -> bool:
        self.calls.append(("unrecord_tx_hash", tx_hash, network))
        if self.outage:
            return False
        self.tx_hashes.discard((tx_hash, network))
        return True

    async def is_payment_id_consumed(self, payment_id: str) -> bool:
        return payment_id in self.payment_ids

    async def is_tx_hash_consumed(self, tx_hash: str, network: str) -> bool:
        return (tx_hash, network) in self.tx_hashes

    # ── pending challenges ───────────────────────────────────────────────
    # Lookups fall through to the gateway's in-memory dict (None here), so
    # the challenge itself is never the thing under test.
    async def store_pending_challenge(self, **kw) -> None:
        self.challenges[kw["payment_id"]] = kw

    async def get_pending_challenge(self, payment_id: str) -> Optional[dict]:
        return None

    async def delete_pending_challenge(self, payment_id: str) -> None:
        self.challenges.pop(payment_id, None)

    # ── payment_logs ─────────────────────────────────────────────────────
    async def insert_pending_payment_log(self, payment_id: str, tool_name: str,
                                         network: str, amount_usdc: str, *,
                                         state: str = "pending", **fields) -> Optional[int]:
        row = {"payment_id": payment_id, "tool_name": tool_name, "network": network,
               "amount_usdc": amount_usdc, "state": state, "error_reason": None}
        row.update(fields)
        self.logs[payment_id] = row
        return len(self.logs)

    async def get_payment_log(self, payment_id: str, columns: str = "") -> Optional[dict]:
        return self.logs.get(payment_id)

    async def update_payment_log_state(self, payment_id: str, state: str, *,
                                       expected_state=None, clear_fields=None,
                                       **fields) -> Optional[int]:
        row = self.logs.get(payment_id)
        if row is None:
            return 0
        if expected_state is not None:
            allowed = (expected_state,) if isinstance(expected_state, str) else tuple(expected_state)
            if row["state"] not in allowed:
                return 0
        row["state"] = state
        row.update(fields)
        for f in clear_fields or ():
            row[f] = None
        return 1

    async def correlate_pending_challenge(self, tool_name, client_ip, user_agent,
                                          tx_hash) -> Optional[str]:
        return None

    async def mark_split_failed(self, payment_id: str, reason: str) -> None:
        row = self.logs.get(payment_id)
        if row is not None:
            row["error_reason"] = f"split_failed: {reason}"

    async def persist_tool_registration(self, tool_dict: dict) -> bool:
        return True


PATCHED_NAMES = (
    "record_payment_id", "record_tx_hash", "unrecord_tx_hash",
    "is_payment_id_consumed", "is_tx_hash_consumed",
    "store_pending_challenge", "get_pending_challenge", "delete_pending_challenge",
    "insert_pending_payment_log", "get_payment_log", "update_payment_log_state",
    "correlate_pending_challenge", "mark_split_failed", "persist_tool_registration",
)


def install(monkeypatch, fake: FakeSupabase) -> None:
    """Patch the service module and every consumer that imported a name
    directly, then flip sb_enabled on for the request paths."""
    import gateway.base
    import gateway.main
    import gateway.routes.tools
    import gateway.services.supabase as sb
    import gateway.stacks
    import gateway.x402

    for name in PATCHED_NAMES:
        monkeypatch.setattr(sb, name, getattr(fake, name))
        for mod in (gateway.routes.tools, gateway.main):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, getattr(fake, name))
    enabled = lambda: True  # noqa: E731
    monkeypatch.setattr(sb, "sb_enabled", enabled)
    monkeypatch.setattr(gateway.routes.tools, "sb_enabled", enabled)
    # gateway.main keeps the disabled stub: its lifespan workers are not
    # part of the contract and would poll the fake forever.


# ── sessions (AGE-207) ───────────────────────────────────────────────────────
# PostgREST routes for the sessions table and its functions, mirroring
# db/migrations/sessions.sql (validated against Postgres 16 before this was
# written). Mounted on a respx router so the service's real HTTP layer runs.

SB_URL = "https://sb.test"


class FakeSessionStore:
    def __init__(self, fake: FakeSupabase) -> None:
        self.fake = fake
        self.rows: dict[str, dict] = {}
        self.rpc_down = False
        self.rpc_calls: list[tuple] = []
        self.now_offset_s = 0.0

    def _now(self):
        from datetime import datetime, timedelta, timezone
        return datetime.now(tz=timezone.utc) + timedelta(seconds=self.now_offset_s)

    @staticmethod
    def _ts(s: str):
        from datetime import datetime, timezone
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def live_for(self, payer: str) -> Optional[dict]:
        cands = [r for r in self.rows.values()
                 if r["payer"] == payer and r["status"] in ("active", "exhausted")]
        cands.sort(key=lambda r: (r["status"] == "active", r["created_at"]), reverse=True)
        return cands[0] if cands else None

    def consume(self, payer: str, cost: str, session_id: Optional[str]) -> dict:
        from decimal import Decimal
        none = {"r_found": False, "r_ok": False, "r_session_id": None,
                "r_max_spend": None, "r_spent": None, "r_expires_at": None}
        s = self.rows.get(session_id) if session_id else self.live_for(payer)
        if s is None or s["status"] not in ("active", "exhausted"):
            return none
        view = {"r_session_id": s["session_id"], "r_max_spend": s["max_spend"],
                "r_spent": s["spent"], "r_expires_at": s["expires_at"]}
        if s["payer"] != payer:
            return {"r_found": True, "r_ok": False, **view}
        if self._ts(s["expires_at"]) <= self._now():
            s["status"] = "expired"
            return {"r_found": False, "r_ok": False, **view}
        spent, cap, c = Decimal(s["spent"]), Decimal(s["max_spend"]), Decimal(cost)
        if spent + c > cap:
            return {"r_found": True, "r_ok": False, **view}
        s["spent"] = str(spent + c)
        s["status"] = "exhausted" if spent + c >= cap else "active"
        return {"r_found": True, "r_ok": True, **view, "r_spent": s["spent"]}

    def release(self, session_id: str, cost: str) -> str:
        from decimal import Decimal
        s = self.rows[session_id]
        s["spent"] = str(max(Decimal(s["spent"]) - Decimal(cost), Decimal(0)))
        if s["status"] == "exhausted":
            s["status"] = "active"
        return s["spent"]

    def expire(self) -> int:
        n = 0
        for s in self.rows.values():
            if s["status"] in ("active", "exhausted") and self._ts(s["expires_at"]) <= self._now():
                s["status"] = "expired"
                n += 1
        return n

    def mount(self, router) -> None:
        import json as _json
        import httpx

        def insert(request):
            row = _json.loads(request.content)
            if any(r["payer"] == row["payer"] and r["status"] == "active" for r in self.rows.values()):
                return httpx.Response(409, json={"code": "23505"})
            self.rows[row["session_id"]] = dict(row)
            return httpx.Response(201, json=[row])

        def select(request):
            p = request.url.params
            rows = list(self.rows.values())
            if "payer" in p:
                rows = [r for r in rows if r["payer"] == p["payer"].removeprefix("eq.")]
            if "status" in p:
                rows = [r for r in rows if r["status"] == p["status"].removeprefix("eq.")]
            if "session_id" in p:
                rows = [r for r in rows if r["session_id"] == p["session_id"].removeprefix("eq.")]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return httpx.Response(200, json=rows[: int(p.get("limit", "50"))])

        def rpc(request, name):
            args = _json.loads(request.content or b"{}")
            self.rpc_calls.append((name, args))
            if self.rpc_down:
                return httpx.Response(503, text="down")
            if name == "consume_session_budget":
                return httpx.Response(200, json=[self.consume(
                    args["p_payer"], args["p_cost"], args.get("p_session_id"))])
            if name == "release_session_budget":
                return httpx.Response(200, json=self.release(args["p_session_id"], args["p_cost"]))
            if name == "expire_sessions":
                return httpx.Response(200, json=self.expire())
            return httpx.Response(404)

        def receipts(request):
            sid = request.url.params.get("session_id", "").removeprefix("eq.")
            rows = [r for r in self.fake.logs.values() if r.get("session_id") == sid]
            return httpx.Response(200, json=rows)

        router.post(f"{SB_URL}/rest/v1/sessions").mock(side_effect=insert)
        router.get(f"{SB_URL}/rest/v1/sessions").mock(side_effect=select)
        router.post(url__regex=rf"{SB_URL}/rest/v1/rpc/(?P<name>\w+)").mock(side_effect=rpc)
        router.get(f"{SB_URL}/rest/v1/payment_logs").mock(side_effect=receipts)
