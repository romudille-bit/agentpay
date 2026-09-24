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

