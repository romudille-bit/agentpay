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

Transfer history comes from the PUBLIC Base JSON-RPC via eth_getLogs — no
API key. (Etherscan V2 was the original source, but Base/chainid-8453 is now
paywalled on free keys: "Free API access is not supported for this chain",
observed 2026-07-28. The gateway's own payment verification already uses
JSON-RPC, so this matches production.) Set BASE_RPC_URL to override the
default https://mainnet.base.org.

Usage:
    python tools/reconcile_prober_spend.py                    # latest sweep
    python tools/reconcile_prober_spend.py --run 2026-07-28   # by date prefix
    python tools/reconcile_prober_spend.py --wallet 0x…       # wallet override

The wallet is normally read from the sweep's ledger entry, but the ledger's
reasoning whitelist DROPS the `wallet` field the prober posts (verified
2026-07-28: reasoning carries receipt/breakdown, wallet=None). Fallbacks, in
order: --wallet flag → PROBER_WALLET env → the funded prober address below
(stable: PROBER_BASE_KEY is a fixed key, so the address never rotates).
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
# The funded prober wallet (PROBER_BASE_KEY's address). Last-resort fallback
# for sweeps whose ledger entry lost the wallet field — matches the
# "run start … | wallet 0x…" line in every prober Railway log.
DEFAULT_PROBER_WALLET = "0xc507d39678309B2389744526A7CD86E236C6C750"


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


BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _rpc(method: str, params: list):
    req = urllib.request.Request(
        BASE_RPC,
        data=json.dumps({"jsonrpc": "2.0", "id": 1,
                         "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if "error" in data:
        print(f"FATAL: RPC error — {data['error']}")
        sys.exit(1)
    return data["result"]


def _block_ts(n: int) -> int:
    return int(_rpc("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)


def wallet_transfers(wallet: str, start: datetime, end: datetime) -> list[dict]:
    """Outbound USDC Transfer events from `wallet` on Base, via eth_getLogs
    against the public RPC. Block range is estimated from Base's ~2s block
    time with a ±300-block margin, then each log's block timestamp is checked
    exactly. Returns rows shaped for probe.reconcile_settlements:
    {"to", "value" (atomic str), "hash", "timeStamp"}."""
    latest = int(_rpc("eth_blockNumber", []), 16)
    latest_ts = _block_ts(latest)

    def est(ts: float) -> int:
        return latest - int((latest_ts - ts) // 2)

    from_block = max(0, est(start.timestamp()) - 300)
    to_block = min(latest, est(end.timestamp()) + 300)
    logs = _rpc("eth_getLogs", [{
        "address": USDC_BASE,
        "fromBlock": hex(from_block), "toBlock": hex(to_block),
        "topics": [TRANSFER_SIG, "0x" + "0" * 24 + wallet[2:].lower()],
    }])
    out = []
    for log in logs:
        ts = _block_ts(int(log["blockNumber"], 16))
        t = datetime.fromtimestamp(ts, tz=timezone.utc)
        if not (start <= t <= end):
            continue
        out.append({
            "to": "0x" + log["topics"][2][-40:],
            "value": str(int(log["data"], 16)),
            "hash": log["transactionHash"],
            "timeStamp": str(ts),
        })
    return out


def main() -> int:
    run_prefix = None
    if "--run" in sys.argv:
        run_prefix = sys.argv[sys.argv.index("--run") + 1]
    wallet_override = None
    if "--wallet" in sys.argv:
        wallet_override = sys.argv[sys.argv.index("--wallet") + 1]

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
    # The ledger's reasoning whitelist drops `wallet`, so the ledger value is
    # normally None — fall through to the override chain.
    wallet = (wallet_override
              or rs.get("wallet")
              or os.environ.get("PROBER_WALLET", "")
              or DEFAULT_PROBER_WALLET)
    if not wallet_override and not rs.get("wallet"):
        print(f"(wallet not on the ledger entry — using {wallet}; "
              f"override with --wallet if this sweep used another key)")
    started = datetime.fromisoformat(str(run["started"]).replace("Z", "+00:00"))
    ended_raw = run.get("ended") or run.get("started")
    ended = datetime.fromisoformat(str(ended_raw).replace("Z", "+00:00"))
    if not breakdown:
        print("Run found but receipt breakdown missing — cannot reconcile")
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
