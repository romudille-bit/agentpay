#!/usr/bin/env python3
"""
tools/probe_fuchss_report.py — buy x402.fuchss.app's paid trust reports for our
own three scored endpoints ($0.005 each, USDC EIP-3009 on Base) and print the
full component breakdown, so we see exactly WHAT their probes record as failures
(timeout vs 5xx vs envelope) behind the public Grade C / ~84% uptime.

Diagnostic for the AGE-108 fuchss investigation (2026-08-12). Paid via our own
SDK — an AgentPay session with a $0.02 cap buys a competitor-adjacent trust
report over x402. Dogfood both ways.

Reads the funded wallet from .env: FLAGSHIP_BASE_KEY (or AGENT_BASE_KEY_TEST)
as the Base payer; any Stellar secret as wallet identity (ephemeral otherwise).

Run:
    ./venv/bin/python tools/probe_fuchss_report.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Load .env (no external dep) ────────────────────────────────────────────────
try:
    for _line in open(os.path.join(ROOT, ".env")):
        _s = _line.strip()
        if _s and not _s.startswith("#") and "=" in _s:
            _k, _, _v = _s.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
except FileNotFoundError:
    pass

sys.path.insert(0, ROOT)
from agentpay import AgentWallet, Session, PaymentFailed  # noqa: E402

base = (os.environ.get("FLAGSHIP_BASE_KEY")
        or os.environ.get("AGENT_BASE_KEY_TEST")
        or os.environ.get("BASE_AGENT_KEY") or "").strip()
if not base:
    print("✗ No funded Base key in .env (FLAGSHIP_BASE_KEY / AGENT_BASE_KEY_TEST / BASE_AGENT_KEY).")
    sys.exit(1)

stellar = (os.environ.get("FLAGSHIP_STELLAR_SECRET")
           or os.environ.get("AGENT_STELLAR_KEY_TEST") or "").strip()
if not stellar:
    from stellar_sdk import Keypair
    stellar = Keypair.random().secret
    print("(no Stellar secret in .env — ephemeral identity; payment settles on Base)")

wallet = AgentWallet(secret_key=stellar, network="mainnet", base_key=base)
print(f"Base payer : {wallet.base_address}")

FUCHSS = "https://x402.fuchss.app/v1/x402-trust"
RESOURCES = [
    "https://agentpay.tools/v1/session/create",
    "https://agentpay.tools/tools/pre_trade_check/call",
    "https://agentpay.tools/tools/verified_route/call",
]

# NOTE: max_per_tool is a CUMULATIVE cap per URL across the session, not
# per-call — at $0.005 x 3 endpoints the cap must be >= $0.015 or the last
# report is refused with BudgetExceeded (learned the hard way, 2026-08-12).
s = Session(wallet=wallet, gateway_url="https://agentpay.tools", max_spend="0.05",
            max_per_tool={FUCHSS: "0.02"})

reports = {}
for resource in RESOURCES:
    print(f"\n═══ Buying trust report ($0.005): {resource}")
    try:
        r = s.call(FUCHSS, {"resource": resource}, chain="base")
        data = r if isinstance(r, dict) else getattr(r, "data", r)
        reports[resource] = data
        print(json.dumps(data, indent=2, default=str)[:4000])
    except PaymentFailed as e:
        print(f"✗ payment failed: {e}")
    except Exception as e:
        print(f"✗ {type(e).__name__}: {e}")

print(f"\nSession spent: {s.spent()}")
out = os.path.join(ROOT, "fuchss_reports.json")
with open(out, "w") as f:
    json.dump(reports, f, indent=2, default=str)
print(f"Full reports saved to {out} — paste or share that file back into the session.")
