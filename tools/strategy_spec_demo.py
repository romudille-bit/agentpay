#!/usr/bin/env python3
"""
tools/strategy_spec_demo.py — produce ONE flagship strategy_spec run locally.

Forces FLAGSHIP_GOAL=strategy_spec and runs the flagship analyst against the
production gateway, using the funded TEST wallet from .env and the REPO SDK
(which has the 0.2.7 external-x402 fixes the published package may not yet have).

Honest routing → verified_route vetting ($0.01) → paid CMC dex_search ($0.01,
token+price+liquidity) → regime-gated mean-reversion spec backtested over 180d
with a buy-and-hold benchmark. Prints the FLAGSHIP_STRATEGY {json} line — that's
the BNB Track-2 deliverable. Spend is capped (FLAGSHIP_MAX_SPEND, default $0.10).

Run:
    ./venv/bin/python tools/strategy_spec_demo.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Load .env ──────────────────────────────────────────────────────────────────
try:
    for _line in open(os.path.join(ROOT, ".env")):
        _s = _line.strip()
        if _s and not _s.startswith("#") and "=" in _s:
            _k, _, _v = _s.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
except FileNotFoundError:
    pass

# Map the funded test wallet → the FLAGSHIP_* names run.py expects.
os.environ.setdefault("FLAGSHIP_STELLAR_SECRET",
                      os.environ.get("AGENT_STELLAR_KEY_TEST", ""))
os.environ.setdefault("FLAGSHIP_BASE_KEY",
                      os.environ.get("AGENT_BASE_KEY_TEST", ""))
os.environ["FLAGSHIP_GOAL"] = "strategy_spec"
os.environ.setdefault("FLAGSHIP_TARGET_TOKEN", "BNB")
os.environ.setdefault("FLAGSHIP_MAX_SPEND", "0.10")
# FORCE production — .env sets AGENTPAY_GATEWAY_URL=localhost for dev, but this
# demo must hit the real gateway (setdefault would lose to the .env value).
os.environ["AGENTPAY_GATEWAY_URL"] = os.environ.get("DEMO_GATEWAY") or "https://agentpay.tools"

if not os.environ.get("FLAGSHIP_BASE_KEY"):
    print("✗ No funded Base key (FLAGSHIP_BASE_KEY / AGENT_BASE_KEY_TEST) in .env.")
    sys.exit(1)

# Repo SDK first (gets the 0.2.7 external-x402 fixes regardless of what's pip-installed).
sys.path.insert(0, ROOT)
from agents.analyst.run import main  # noqa: E402

sys.exit(main())
