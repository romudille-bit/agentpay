#!/usr/bin/env python3
"""
verify_ledger_legs.py — AGE-142: backfill / re-run chain verification of the
ledger's off-gateway receipt legs from a laptop.

The gateway runs the same verifier in the background every 6h
(gateway/services/leg_verifier.py, 12 runs per cycle). This CLI is for the
first backfill (~90 receipt-derived runs) and for checking a specific run
without waiting for the cycle. Reads SUPABASE_URL / SUPABASE_KEY / BASE_RPC_URL
from .env via gateway.config.

    source venv/bin/activate
    python3 tools/verify_ledger_legs.py --dry-run          # show what would match
    python3 tools/verify_ledger_legs.py                    # verify + write cache
    python3 tools/verify_ledger_legs.py --run 2026-08-24   # one run (prefix match)
    python3 tools/verify_ledger_legs.py --force            # re-check already-checked runs

Then load https://agentpay.tools/ledger.json and read totals.verified_share.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from gateway.services import leg_verifier as lv  # noqa: E402
from gateway.services import supabase as sb      # noqa: E402

# Wallet fallbacks when a run meta carries no `wallet` (older rows): the
# flagship analyst and the prober. Override with --wallet.
DEFAULT_WALLETS = [
    "0xe1601C10B8d4DbF71E0c592B779520380174bc3A",  # flagship analyst
    "0xc507d39678309B2389744526A7CD86E236C6C750",  # prober
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="only runs whose run_at starts with this prefix")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--force", action="store_true", help="re-check runs already marked checked")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wallet", action="append", help="fallback wallet(s) for metas without one")
    a = ap.parse_args()
    if not sb.sb_enabled():
        print("SUPABASE_URL / SUPABASE_KEY missing (.env)"); return 1

    metas = await sb.fetch_flagship_runs(limit=a.limit)
    existing = {} if a.force else await sb.fetch_leg_verifications()
    hints = await sb.fetch_payto_hints()
    fallbacks = a.wallet or DEFAULT_WALLETS
    todo = [m for m in metas
            if (not a.run or str(m.get("run_at", "")).startswith(a.run))
            and lv.needs_check(m, existing)]
    print(f"{len(metas)} runs loaded · {len(todo)} to check · {len(hints)} payTo hints")

    total_legs = matched = written = 0
    by_method: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for m in todo:
            legs = lv.paid_legs((m.get("receipt") or {}).get("breakdown") or [])
            total_legs += len(legs)
            try:
                rows = await lv.verify_run(m, fallbacks, hints, client)
            except Exception as e:
                print(f"  ! {m.get('run_at')}: {e}")
                continue
            hits = [r for r in rows if r["leg_index"] >= 0]
            matched += len(hits)
            for r in hits:
                by_method[r["method"]] = by_method.get(r["method"], 0) + 1
            kind = ((m.get("objective") or {}).get("kind")) or "?"
            print(f"  {str(m.get('run_at'))[:19]} {kind:12} {len(hits):2}/{len(legs):2} legs matched"
                  + (f"  [{', '.join(sorted({r['method'] for r in hits}))}]" if hits else ""))
            if rows and not a.dry_run:
                if await sb.upsert_leg_verifications(rows):
                    written += len(rows)
                else:
                    print("    ! upsert failed")
    print(f"\nlegs {total_legs} · matched {matched} ({(100*matched/total_legs if total_legs else 0):.0f}%) "
          f"· by method {by_method} · rows written {written}{' (dry run)' if a.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
