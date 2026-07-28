#!/usr/bin/env python3
"""
reconcile_prober_spend.py — AGE-88.

Resolves a prober sweep's `uncertain_settlement` entries against on-chain
reality. The SDK fails closed (AGE-56): once a signed EIP-3009 authorization
is transmitted, the spend is recorded even if the seller answers non-200 —
so a sweep's receipt can carry spend nobody can substantiate.

The answer is payer-observable: `transferWithAuthorization` emits a standard
ERC-20 Transfer event FROM our wallet regardless of who submits the tx, so
the wallet's USDC transfer history on Base is ground truth. This tool pulls
it via Etherscan V2 (chainid=8453) and matches it against the run receipt
with the pure matcher in agents/prober/probe.py.

Interpretation:
  confirmed           — the SDK already saw a 200; transfer anchored to it
  settled_on_chain    — seller SETTLED a payment for a request they rejected.
                        Money gone, nothing back: real took-payment evidence,
                        currently discarded as buyer-side noise (AGE-87 RC4).
  no_onchain_evidence — no matching transfer: the auth was never settled;
                        the recorded spend for this entry was conservative.

Usage:
    export ETHERSCAN_API_KEY=<key>            # same key the gateway uses
    python tools/reconcile_prober_spend.py                    # latest sweep
    python tools/reconcile_prober_spend.py --run 2026-07-28   # by date prefix
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.prober import probe  # noqa: E402

GATEWAY = os.environ.get("AGENTPAY_GATEWAY_URL", "https://agentpay.tools")
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
UA = {"User-Agent": "Mozilla/5.0 (compatible; x402-client)"}


def _get(url: str) -> dict:
    with urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.loads(r.read().decode())


def latest_sweep(run_prefix: str | None) -> dict | None:
    data = _get(f"{GATEWAY}/ledger.json")
    sweeps = []
    for run in data.get("runs") or []:
        rs = run.get("reasoning") or {}
        if ((rs.get("objective") or {}).get("kind")) != "probe_sweep":
            continue
        if run_prefix and not str(run.get("started", "")).startswith(run_prefix):
            continue
        sweeps.append(run)
    sweeps.sort(key=lambda r: r.get("started") or "", reverse=True)
    return sweeps[0] if sweeps else None


def wallet_transfers(wallet: str, start: datetime, end: datetime) -> list[dict]:
    key = os.environ.get("ETHERSCAN_API_KEY", "")
    if not key:
        print("FATAL: ETHERSCAN_API_KEY is required (Etherscan V2, any chain)")
        sys.exit(1)
    q = urllib.parse.urlencode({
        "chainid": "8453", "module": "account", "action": "tokentx",
        "contractaddress": USDC_BASE, "address": wallet,
        "sort": "desc", "apikey": key,
    })
    data = _get(f"https://api.etherscan.io/v2/api?{q}")
    out = []
    for t in data.get("result") or []:
        if str(t.get("from", "")).lower() != wallet.lower():
            continue
        try:
            ts = datetime.fromtimestamp(int(t["timeStamp"]), tz=timezone.utc)
        except (ValueError, TypeError, KeyError):
            continue
        if start <= ts <= end:
            out.append(t)
    return out


def main() -> int:
    run_prefix = None
    if "--run" in sys.argv:
        run_prefix = sys.argv[sys.argv.index("--run") + 1]

    run = latest_sweep(run_prefix)
    if not run:
        print("No matching probe_sweep found on the ledger")
        return 1
    rs = run.get("reasoning") or {}
    receipt = ((rs.get("findings") or {}).get("probe_sweep") or {}).get("receipt") \
        or rs.get("receipt") or {}
    breakdown = receipt.get("breakdown") or []
    # Fall back to the top-level receipt shape used by the PROBER_SWEEP line.
    if not breakdown and isinstance(run.get("reasoning", {}).get("receipt"), dict):
        breakdown = run["reasoning"]["receipt"].get("breakdown") or []
    wallet = (rs.get("wallet")
              or (run.get("reasoning") or {}).get("wallet") or "")
    started = datetime.fromisoformat(str(run["started"]).replace("Z", "+00:00"))
    ended_raw = run.get("ended") or run.get("started")
    ended = datetime.fromisoformat(str(ended_raw).replace("Z", "+00:00"))
    if not (breakdown and wallet):
        print("Run found but receipt breakdown / wallet missing — cannot reconcile")
        return 1

    print(f"\nSweep {run['started']} | wallet {wallet}")
    transfers = wallet_transfers(wallet, started - timedelta(minutes=5),
                                 ended + timedelta(hours=2))
    print(f"{len(transfers)} outbound USDC transfer(s) in window\n")

    resolved = probe.reconcile_settlements(breakdown, transfers)
    settled_anyway = spent = unspent = 0
    for r in resolved:
        mark = {"confirmed": "✓", "settled_on_chain": "⚠",
                "no_onchain_evidence": "·"}[r["resolution"]]
        print(f"  {mark} {r['resolution']:20} {str(r['cost']):>10}  "
              f"{str(r['tool'])[:70]}"
              + (f"  tx {r['tx_hash'][:14]}…" if r.get("tx_hash") else ""))
        if r["resolution"] == "settled_on_chain":
            settled_anyway += 1
        if r["resolution"] == "no_onchain_evidence":
            unspent += 1

    print(f"\n⚠ settled-but-rejected: {settled_anyway} — the seller took the "
          f"money for a request they refused. Real took-payment evidence; "
          f"promote to scoring via AGE-86's payment_rejected outcome.")
    print(f"· no on-chain evidence: {unspent} — those auths were never "
          f"settled; the receipt over-counts by their sum (fail-closed by "
          f"design, AGE-56).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
