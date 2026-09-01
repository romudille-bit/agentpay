#!/usr/bin/env python3
"""Verify every _EXCHANGE_WALLETS entry: EOA status + last-tx age.

Uses the Etherscan V2 API (chainid=1). Key from ETHERSCAN_API_KEY env
(falls back to gateway config / .env). Pure stdlib — no deps.

Usage:
    ./venv/bin/python tools/audit_exchange_wallets.py [--stale-months N] [--json]

Output: one line per address (exchange, label check, EOA/contract,
last-tx date, verdict), then a summary. Exit code 1 if any entry is a
contract or stale beyond the threshold — so a cron/CI run fails loudly.

Written for the `exchange-wallet-audit` scheduled task (2026-09-01) so
future audits are exact instead of best-effort page scraping.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME = REPO_ROOT / "gateway" / "services" / "tools_runtime.py"


def _load_wallets() -> dict[str, str]:
    """Parse _EXCHANGE_WALLETS via ast — avoids importing gateway deps (httpx etc.)."""
    tree = ast.parse(_RUNTIME.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_EXCHANGE_WALLETS":
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_EXCHANGE_WALLETS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(f"_EXCHANGE_WALLETS not found in {_RUNTIME}")


_EXCHANGE_WALLETS = _load_wallets()

API = "https://api.etherscan.io/v2/api"
RATE_DELAY = 0.25  # free tier: 5 req/s; 2 calls per address


def _api_key() -> str:
    key = os.environ.get("ETHERSCAN_API_KEY", "")
    if key:
        return key
    try:
        from gateway.config import settings  # type: ignore

        return settings.ETHERSCAN_API_KEY or ""
    except Exception:
        return ""


def _get(params: dict, key: str) -> dict:
    params = {"chainid": "1", "apikey": key, **params}
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "agentpay-wallet-audit/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def check_address(addr: str, key: str) -> dict:
    out: dict = {"address": addr}

    code = _get({"module": "proxy", "action": "eth_getCode", "address": addr, "tag": "latest"}, key)
    out["is_contract"] = code.get("result", "0x") not in ("0x", "0x0", None)
    time.sleep(RATE_DELAY)

    txs = _get(
        {
            "module": "account",
            "action": "txlist",
            "address": addr,
            "page": "1",
            "offset": "1",
            "sort": "desc",
        },
        key,
    )
    time.sleep(RATE_DELAY)
    result = txs.get("result") or []
    if isinstance(result, list) and result:
        ts = int(result[0]["timeStamp"])
        out["last_tx"] = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        out["last_tx"] = None  # no txs returned (or API message in `result`)
        out["note"] = txs.get("message", "")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-months", type=int, default=6)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--limit", type=int, default=0, help="check only the first N addresses (0 = all)")
    ap.add_argument("--offset", type=int, default=0, help="skip the first N addresses (for chunked runs)")
    args = ap.parse_args()

    key = _api_key()
    if not key:
        print("ERROR: no ETHERSCAN_API_KEY (env or gateway config).", file=sys.stderr)
        return 2

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.stale_months * 30)
    rows, problems = [], 0

    entries = list(_EXCHANGE_WALLETS.items())[args.offset:]
    if args.limit:
        entries = entries[: args.limit]

    for addr, exchange in entries:
        try:
            info = check_address(addr, key)
        except Exception as exc:  # network blip: report, don't abort the sweep
            rows.append({"address": addr, "exchange": exchange, "verdict": f"ERROR: {exc}"})
            problems += 1
            if not args.json:
                print(f"{exchange:<12} {addr}  ERROR: {exc}", flush=True)
            continue

        info["exchange"] = exchange
        if info["is_contract"]:
            info["verdict"] = "CONTRACT — should not be in the EOA hot-wallet list"
            problems += 1
        elif info["last_tx"] is None:
            info["verdict"] = f"NO TX DATA ({info.get('note', '')}) — manual check"
            problems += 1
        elif info["last_tx"] < cutoff:
            info["verdict"] = f"STALE — last tx {info['last_tx']:%Y-%m-%d}"
            problems += 1
        else:
            info["verdict"] = f"ok — last tx {info['last_tx']:%Y-%m-%d}"
        rows.append(info)
        if not args.json:  # stream progress so partial output survives interrupts
            print(f"{exchange:<12} {addr}  {info['verdict']}", flush=True)

    if args.json:
        print(json.dumps(rows, default=str, indent=2))
    else:
        print(f"\n{len(rows)} addresses checked, {problems} problem(s), "
              f"stale threshold {args.stale_months} months.")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
