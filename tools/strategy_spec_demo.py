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

import io  # noqa: E402
import json as _json  # noqa: E402
from agents.analyst.run import main  # noqa: E402


class _Tee(io.TextIOBase):
    """Pass run logs through to the terminal, but capture (and hide) the big
    machine-readable FLAGSHIP_STRATEGY {json} line so the console stays clean."""

    def __init__(self, real):
        self._real = real
        self.captured = None

    def write(self, s):
        if s.startswith("FLAGSHIP_STRATEGY "):
            self.captured = s[len("FLAGSHIP_STRATEGY "):].strip()
            return len(s)
        return self._real.write(s)

    def flush(self):
        self._real.flush()


def _pct(v):
    return "—" if v is None else f"{float(v):+.2f}%"


def _summary(blob):
    spec = blob.get("strategy_spec", {})
    sig = spec.get("signal", {})
    ex = spec.get("execution", {})
    tok = (spec.get("universe") or [{}])[0]
    bt = (spec.get("backtest") or {}).get("results") or {}
    bm = bt.get("benchmark") or {}
    edge = bt.get("edge") or {}
    vet = blob.get("vetting") or {}
    cat = vet.get("catalog") or {}
    rec = vet.get("recommendation") or {}
    rc = blob.get("receipt") or {}
    bp = bt.get("best_params") or {}

    def row(label, a, b, c):
        return f"    {label:<14}{a:>12}{b:>12}{c:>14}"

    L = []
    L.append("")
    L.append("═" * 62)
    L.append(f"  STRATEGY SPEC — {spec.get('name', '?')}")
    L.append("═" * 62)
    L.append(f"  Token         : {tok.get('symbol')} ({tok.get('name')}) · {tok.get('network')}")
    L.append(f"  Regime        : Fear&Greed {sig.get('fear_greed')} → entry_bias = {sig.get('entry_bias')}")
    liq = ex.get("liquidity_usd") or 0
    mp = ex.get("max_position_usd") or 0
    L.append(f"  Pool liquidity: ${liq:,.0f}   ·   max position ${mp:,.0f}")
    L.append(f"  Vetted source : {rec.get('name')} ({rec.get('payers30d')} payers) — swept "
             f"{cat.get('scanned')} listings, collapsed {cat.get('sybil_collapsed')} sybils → "
             f"{cat.get('real_providers')} real")
    L.append("─" * 62)
    L.append(f"  BACKTEST 180d  (best of {bt.get('combos_tested')}: "
             f"fear≤{bp.get('fear_entry')} / exit≥{bp.get('greed_exit')} / hold≤{bp.get('hold_days_max')}d)")
    L.append(row("", "strategy", "buy & hold", "edge"))
    L.append(row("return", _pct(bt.get("total_return_pct")), _pct(bm.get("total_return_pct")),
                 _pct(edge.get("excess_return_pct"))))
    es = edge.get("excess_sharpe")
    L.append(row("Sharpe", str(bt.get("sharpe")), str(bm.get("sharpe")),
                 (f"+{es}" if (es or 0) > 0 else str(es))))
    L.append(row("max drawdown", f"{bt.get('max_drawdown_pct')}%", f"{bm.get('max_drawdown_pct')}%",
                 f"{edge.get('drawdown_reduction_pct')}% safer"))
    verdict = ("BEATS HOLD ✓" if edge.get("beats_hold_sharpe")
               and (edge.get("beats_hold_return") or edge.get("lower_drawdown")) else "mixed vs hold")
    L.append(f"    win rate {bt.get('win_rate_pct')}% · {bt.get('n_trades')} trades   →   {verdict}")
    L.append("─" * 62)
    L.append(f"  Spend: {rc.get('spent')} of {rc.get('budget')} ({rc.get('calls')} calls) — "
             f"free intel + $0.01 verified_route + $0.01 CMC dex_search")
    L.append("═" * 62)
    return "\n".join(L)


if __name__ == "__main__":
    _real = sys.stdout
    tee = _Tee(_real)
    sys.stdout = tee
    try:
        rc = main()
    finally:
        sys.stdout = _real

    if not tee.captured:
        print("\n(no strategy_spec produced — see the run log above)")
        sys.exit(rc or 1)

    blob = _json.loads(tee.captured)
    out_path = os.path.join(ROOT, "strategy_spec_latest.json")
    with open(out_path, "w") as f:
        _json.dump(blob, f, indent=2)

    print(_summary(blob))
    print(f"  Full backtestable spec saved → {out_path}\n")
    sys.exit(rc)
