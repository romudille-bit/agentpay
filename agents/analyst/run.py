#!/usr/bin/env python3
"""
run.py — the AgentPay flagship analyst agent.

An autonomous market analyst that lives on AgentPay's own payment rails as a
REAL customer: it installs the published SDK (`pip install "agentpay-x402[base]"`),
talks to the production gateway over HTTP, holds its own funded wallet, and
operates under a hard per-run budget cap it cannot exceed.

Each run:
  1. PLAN     — decide what to look at (free intel + paid pre_trade_check on majors)
  2. ESTIMATE — price the whole plan via POST /v1/plan/estimate BEFORE spending
  3. EXECUTE  — free tools first, then buy verdicts while budget remains
  4. PUBLISH  — a market note + the spending receipt, as JSON on stdout
                (Railway logs are the v1 publish surface; /ledger reads
                payment_logs server-side)

Identity & config (env):
  FLAGSHIP_STELLAR_SECRET  — persistent Stellar secret (identity; unfunded is fine)
  FLAGSHIP_BASE_KEY        — persistent Base/EVM key (0x..; fund this with USDC)
  FLAGSHIP_MAX_SPEND       — hard cap per run in USDC (default "0.25")
  FLAGSHIP_SYMBOLS         — comma list for paid verdicts (default "BTC,ETH")
  AGENTPAY_GATEWAY_URL     — override gateway (default https://agentpay.tools)

Exit codes: 0 = note published; 1 = run failed (Railway cron surfaces it).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

GATEWAY = os.environ.get("AGENTPAY_GATEWAY_URL", "https://agentpay.tools")
TRADE_SIZE_USD = 25_000   # the notional the verdicts are priced at

# Production (Railway) pip-installs agentpay-x402; a local dev run from the
# repo has neither that nor the repo root on sys.path (python puts the
# SCRIPT's directory there, not the cwd). Fall back to the repo checkout.
try:
    import agentpay  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# strategy.py is a sibling module. Works both as a script (its dir is on path)
# and as a package import (tests do `from agents.analyst import strategy`).
try:
    from agents.analyst import strategy
except ModuleNotFoundError:
    import strategy  # type: ignore


def log(msg: str) -> None:
    print(f"[analyst] {msg}", flush=True)


# ── Note composition (pure — unit-tested) ─────────────────────────────────────

def regime_line(fear_greed: dict | None, funding: dict | None) -> str:
    """One-line market regime from free intel. Defensive on missing data."""
    parts = []
    fg = (fear_greed or {}).get("value")
    fg_label = (fear_greed or {}).get("value_classification")
    if fg is not None:
        parts.append(f"Fear & Greed {fg} ({fg_label})")
    rates = (funding or {}).get("rates") or []
    if rates:
        bearish = sum(1 for r in rates if r.get("sentiment") == "bearish")
        bullish = sum(1 for r in rates if r.get("sentiment") == "bullish")
        if bearish > bullish:
            parts.append("funding leans crowded-long (bearish signal)")
        elif bullish > bearish:
            parts.append("funding leans crowded-short (bullish signal)")
        else:
            parts.append("funding unremarkable")
    return "; ".join(parts) if parts else "regime data unavailable"


def _human_usd(n: float | int | None) -> str | None:
    """Compact USD: 23_800_000_000 → '$23.8B'."""
    if not isinstance(n, (int, float)):
        return None
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"${n / div:.1f}{unit}"
    return f"${n:.0f}"


def news_summary(news: dict | None) -> dict | None:
    """Compress crypto_news headlines into count + net sentiment + top story."""
    headlines = (news or {}).get("headlines") or []
    if not headlines:
        return None
    bull = sum(1 for h in headlines if h.get("sentiment") == "bullish")
    bear = sum(1 for h in headlines if h.get("sentiment") == "bearish")
    net = "bullish" if bull > bear else "bearish" if bear > bull else "mixed"
    top = max(headlines, key=lambda h: h.get("score") or 0).get("title")
    return {"count": len(headlines), "net_sentiment": net,
            "bullish": bull, "bearish": bear, "top_headline": top}


def _tvl_total(tvl: dict | list | None) -> float | None:
    """Total DeFi TVL from defi_tvl, tolerant of single-protocol or top-N shapes."""
    if isinstance(tvl, dict):
        if isinstance(tvl.get("tvl"), (int, float)):
            return float(tvl["tvl"])
        items = tvl.get("protocols")
        if isinstance(items, list):
            return float(sum(p.get("tvl", 0) for p in items if isinstance(p, dict)))
    if isinstance(tvl, list):
        return float(sum(p.get("tvl", 0) for p in tvl if isinstance(p, dict)))
    return None


def context_line(news: dict | None, gas: dict | None, tvl: dict | list | None) -> str:
    """One-line macro context from the curated free tools (defensive on missing)."""
    parts: list[str] = []
    ns = news_summary(news)
    if ns:
        parts.append(f"{ns['count']} headlines (net {ns['net_sentiment']})")
    if gas and gas.get("standard_gwei") is not None:
        parts.append(f"ETH gas {gas['standard_gwei']} gwei")
    total = _tvl_total(tvl)
    if total is not None:
        ch = (tvl or {}).get("change_1d") if isinstance(tvl, dict) else None
        chs = f" ({ch:+.1f}% 24h)" if isinstance(ch, (int, float)) else ""
        parts.append(f"DeFi TVL {_human_usd(total)}{chs}")
    return "; ".join(parts)


def compose_note(
    run_at: str,
    regime: str,
    verdicts: dict[str, dict],
    skipped: dict[str, str],
    context: str = "",
) -> str:
    """Render the daily market note. `verdicts` maps symbol → pre_trade_check
    data; `skipped` maps symbol → reason; `context` is the optional free-intel
    macro line (news/gas/DeFi)."""
    lines = [
        f"AgentPay flagship analyst — {run_at}",
        f"Regime: {regime}",
    ]
    if context:
        lines.append(f"Context: {context}")
    lines += [
        "",
        f"Long-entry check at ${TRADE_SIZE_USD:,} notional:",
    ]
    for sym, v in verdicts.items():
        factors = v.get("factors", {})
        worst = [
            f"{name}: {f.get('reason', '?')}"
            for name, f in factors.items()
            if f.get("level") in ("caution", "avoid")
        ]
        detail = f" ({'; '.join(worst)})" if worst else ""
        lines.append(f"  {sym}: {v.get('verdict', '?').upper()}{detail}")
    for sym, why in skipped.items():
        lines.append(f"  {sym}: skipped ({why})")
    return "\n".join(lines)


# ── Goal rotation ─────────────────────────────────────────────────────────────
# AgentPay does NOT reason about trades. It gives an agent budgeted, priced,
# receipted access to data tools. To demonstrate that honestly — with real
# day-to-day variation and zero fabricated "judgment" — the flagship asks a
# DIFFERENT real question each run and lets the live data differ. Four goals
# rotate deterministically by date; an honest mix means two are paid (a real
# on-chain receipt) and two are free (showing the free tier), so not every day
# spends. Nothing here decides a trade — the pre_trade_check verdict is a tool
# output (a rules-based safety screen the gateway computes from real feeds).

ALT_PAIRS = [["SOL", "AVAX"], ["ARB", "OP"], ["LINK", "UNI"], ["DOGE", "ADA"]]

# Which sub-tool each pre_trade_check factor comes from — used to show that the
# one $0.01 call fans out to several real data sources (not opaque rent).
_FACTOR_TOOL = {
    "liquidity": "orderbook_depth",
    "carry":     "funding_rates",
    "crowding":  "open_interest",
    "security":  "token_security",
}


def _pretrade_spec(symbols: list[str]) -> dict:
    gt = f"Screen {', '.join(symbols)} for safe long entry at ${TRADE_SIZE_USD:,} position size"
    return {
        "kind": "pre_trade",
        "goal_text": gt,
        "free_tools": [
            ("fear_greed_index", {}), ("funding_rates", {}), ("market_snapshot", {}),
            ("crypto_news", {"currencies": ",".join(symbols)}),
            ("gas_tracker", {}), ("defi_tvl", {}),
        ],
        "paid_symbols": list(symbols),
        "objective": {"kind": "pre_trade", "goal_text": gt, "symbols": list(symbols),
                      "trade_size_usd": TRADE_SIZE_USD, "side": "long"},
    }


def goal_pretrade_majors(day: int, override: list[str] | None) -> dict:
    return _pretrade_spec(override or ["BTC", "ETH"])


def goal_pretrade_alts(day: int, override: list[str] | None) -> dict:
    return _pretrade_spec(override or ALT_PAIRS[(day // 4) % len(ALT_PAIRS)])


def goal_regime_brief(day: int, override: list[str] | None) -> dict:
    gt = "Read the market regime — sentiment, funding, catalysts, gas, DeFi"
    return {
        "kind": "regime", "goal_text": gt,
        "free_tools": [
            ("fear_greed_index", {}), ("funding_rates", {}),
            ("crypto_news", {"currencies": "BTC,ETH"}), ("defi_tvl", {}),
            ("gas_tracker", {}), ("market_snapshot", {}),
        ],
        "paid_symbols": [],
        "objective": {"kind": "regime", "goal_text": gt},
    }


def goal_crowding_watch(day: int, override: list[str] | None) -> dict:
    syms = override or ["BTC", "ETH"]
    gt = f"Watch perp positioning on {', '.join(syms)} — open interest, funding, book depth"
    free = [("funding_rates", {})]
    for sym in syms:
        free.append(("open_interest", {"symbol": sym}))
        free.append(("orderbook_depth", {"symbol": sym}))
    return {
        "kind": "crowding", "goal_text": gt,
        "free_tools": free, "paid_symbols": [],
        "objective": {"kind": "crowding", "goal_text": gt, "symbols": list(syms)},
    }


def goal_strategy_spec(day: int, override: list[str] | None) -> dict:
    """Flagship v2 (hackathon) — produce a backtestable strategy spec.

    Free intel for prices/regime; then the buyer-side trust step (verified_route)
    to vet the marketplace; then PAID CMC DEX data (no free equivalent) for the
    target token. Honest routing throughout: pay only what isn't free.
    Force-only (FLAGSHIP_GOAL=strategy_spec) — never auto-rotates, never spends
    on a random day."""
    syms = override or ["BTC", "ETH", "BNB"]
    target = (os.environ.get("FLAGSHIP_TARGET_TOKEN", "").strip() or "BNB").upper()
    gt = (f"Build a backtestable strategy spec for {target}: free regime intel + "
          f"vetted (verified_route) CMC DEX data")
    return {
        "kind": "strategy",
        "goal_text": gt,
        "free_tools": [
            ("fear_greed_index", {}), ("funding_rates", {}),
            ("market_snapshot", {}), ("crypto_news", {"currencies": ",".join(syms)}),
        ],
        "paid_symbols": [],                 # paid path = verified_route + CMC (in the branch)
        "target_token": target,
        "objective": {"kind": "strategy", "goal_text": gt, "target_token": target},
    }


_GOAL_BUILDERS = {
    "pretrade_majors": goal_pretrade_majors,
    "regime_brief":    goal_regime_brief,
    "pretrade_alts":   goal_pretrade_alts,
    "crowding_watch":  goal_crowding_watch,
    "strategy_spec":   goal_strategy_spec,   # force-only; NOT in _ROTATION
}
# Interleaves paid/free across the 4-day cycle (honest mix).
_ROTATION = ["pretrade_majors", "regime_brief", "pretrade_alts", "crowding_watch"]


def select_goal(day_ordinal: int, force: str = "",
                symbols_override: list[str] | None = None) -> dict:
    """Pick the day's goal. PURE. `force` (FLAGSHIP_GOAL) pins a goal for
    demos/testing; otherwise rotate by date. `symbols_override` (FLAGSHIP_SYMBOLS)
    overrides the symbols of a symbol-based goal."""
    name = force if force in _GOAL_BUILDERS else _ROTATION[day_ordinal % len(_ROTATION)]
    spec = _GOAL_BUILDERS[name](day_ordinal, symbols_override)
    spec["name"] = name
    return spec


def _funding_bias(funding: dict | None) -> str | None:
    rates = (funding or {}).get("rates") or []
    if not rates:
        return None
    bear = sum(1 for r in rates if r.get("sentiment") == "bearish")
    bull = sum(1 for r in rates if r.get("sentiment") == "bullish")
    return "crowded-long" if bear > bull else "crowded-short" if bull > bear else "balanced"


def compact_verdict(v: dict) -> dict:
    """pre_trade_check data → verdict + the sub-tools it fanned out to, with each
    sub-tool's real reading. This is what makes the $0.01 legible: one call =
    several live data sources synthesized."""
    subtools = []
    for factor, f in (v.get("factors") or {}).items():
        subtools.append({
            "tool":    _FACTOR_TOOL.get(factor, factor),
            "factor":  factor,
            "level":   (f or {}).get("level"),
            "reading": (f or {}).get("reason"),
        })
    return {"verdict": v.get("verdict"), "subtools": subtools}


def build_findings(kind: str, intel_calls: list[dict], verdicts: dict[str, dict]) -> dict:
    """Structured 'what came back' for the run, by goal kind. PURE.

    intel_calls is an ordered list of {tool, params, data} (data may be None)."""
    def find(tool):
        return next((c["data"] for c in reversed(intel_calls) if c["tool"] == tool), None)

    if kind == "pre_trade":
        return {"verdicts": {sym: compact_verdict(v) for sym, v in verdicts.items()}}

    if kind == "regime":
        fg, fr = find("fear_greed_index"), find("funding_rates")
        ns = news_summary(find("crypto_news"))
        tvl = find("defi_tvl")
        # defi_tvl(no args) returns a top_protocols leaderboard (incl. CEXs), not
        # a single total — surface the largest by name+TVL rather than a misleading
        # sum. Fall back to a real total only when the tool returned one.
        top = (tvl or {}).get("top_protocols") if isinstance(tvl, dict) else None
        defi_top = None
        if isinstance(top, list) and top:
            lead = max(top, key=lambda p: p.get("tvl", 0) if isinstance(p, dict) else 0)
            defi_top = {"name": lead.get("name"), "tvl": lead.get("tvl")}
        return {"regime": {
            "fear_greed":       (fg or {}).get("value"),
            "fear_greed_label": (fg or {}).get("value_classification"),
            "funding_bias":     _funding_bias(fr),
            "headlines":        (ns or {}).get("count"),
            "news_sentiment":   (ns or {}).get("net_sentiment"),
            "gas_gwei":         (find("gas_tracker") or {}).get("standard_gwei"),
            "defi_tvl_usd":     _tvl_total(tvl),
            "defi_top":         defi_top,
        }}

    if kind == "crowding":
        rows: dict[str, dict] = {}
        for c in intel_calls:
            sym = (c.get("params") or {}).get("symbol")
            if not sym:
                continue
            d = c.get("data") or {}
            row = rows.setdefault(sym, {})
            if c["tool"] == "open_interest":
                row["oi_usd"] = d.get("total_oi_usd")
                row["oi_change_24h_pct"] = d.get("oi_change_24h_pct")
                row["long_short_ratio"] = d.get("long_short_ratio")
            elif c["tool"] == "orderbook_depth":
                row["spread_pct"] = d.get("spread_pct")
        return {"crowding": rows, "funding_bias": _funding_bias(find("funding_rates"))}

    return {}


# ── The run ───────────────────────────────────────────────────────────────────

def main() -> int:
    from agentpay import AgentWallet, Session, PaymentFailed, RefundPending

    stellar_secret = os.environ.get("FLAGSHIP_STELLAR_SECRET", "")
    base_key       = os.environ.get("FLAGSHIP_BASE_KEY", "")
    if not (stellar_secret and base_key):
        log("FATAL: FLAGSHIP_STELLAR_SECRET and FLAGSHIP_BASE_KEY are required")
        return 1

    max_spend = os.environ.get("FLAGSHIP_MAX_SPEND", "0.25")
    override = [s.strip().upper() for s in
                os.environ.get("FLAGSHIP_SYMBOLS", "").split(",") if s.strip()] or None
    force_goal = os.environ.get("FLAGSHIP_GOAL", "").strip()

    day_ordinal = datetime.now(tz=timezone.utc).date().toordinal()
    spec = select_goal(day_ordinal, force=force_goal, symbols_override=override)
    objective = dict(spec["objective"])
    objective["cap_usdc"] = str(max_spend)

    wallet = AgentWallet(secret_key=stellar_secret, network="mainnet", base_key=base_key)
    s = Session(wallet=wallet, gateway_url=GATEWAY, max_spend=max_spend)
    run_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_at_iso = datetime.now(tz=timezone.utc).isoformat()
    log(f"run start {run_at} | goal {spec['name']} | wallet {wallet.base_address} | cap ${max_spend}")
    log(f"goal: {spec['goal_text']}")

    paid_symbols = list(spec["paid_symbols"])
    free_step_names = [t for t, _ in spec["free_tools"]]

    # 1-2. PLAN + ESTIMATE — price everything before spending a cent.
    # estimate_plan prices registry tools (verified_route included); external CMC
    # x402 calls aren't in the registry, so for the strategy goal the plan covers
    # the vetting step and the CMC legs are gated live by the session cap.
    if spec["kind"] == "strategy":
        plan_steps = free_step_names + ["verified_route"]
    else:
        plan_steps = free_step_names + ["pre_trade_check"] * len(paid_symbols)
    plan = s.estimate_plan(plan_steps)
    log(f"plan: total ${plan['total_usdc']} for {len(plan_steps)} steps | "
        f"fits budget: {plan.get('fits_budget')}")
    if spec["kind"] != "strategy" and plan.get("fits_budget") is False:
        # Trim paid steps until the plan fits — the cap is the law. Free intel
        # is never trimmed (it costs nothing against the cap).
        while paid_symbols and plan.get("fits_budget") is False:
            paid_symbols.pop()
            plan = s.estimate_plan(free_step_names + ["pre_trade_check"] * len(paid_symbols))
        log(f"plan trimmed to fit: {len(paid_symbols)} paid verdicts")

    # 3. EXECUTE — free tools first (these cost $0 and never settle on-chain)
    intel_calls: list[dict] = []
    free_used: list[str] = []
    for tool, params in spec["free_tools"]:
        try:
            data = s.call(tool, params).data
            intel_calls.append({"tool": tool, "params": params, "data": data})
            free_used.append(tool)
        except Exception as e:
            log(f"free tool {tool} failed: {e}")
            intel_calls.append({"tool": tool, "params": params, "data": None})

    def _last(tool):
        return next((c["data"] for c in reversed(intel_calls) if c["tool"] == tool), None)

    # Flagship v2 (hackathon): strategy goal has its own paid path —
    # verified_route vetting + CMC consume → backtestable strategy spec.
    if spec["kind"] == "strategy":
        return run_strategy(s, spec, intel_calls, run_at, run_at_iso,
                            wallet, max_spend, objective, plan)

    # Paid verdicts (only on pre_trade goals) — stop the moment the cap says stop
    verdicts: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    for sym in paid_symbols:
        if s.would_exceed(s.tool_cost_usd("pre_trade_check") or Decimal("0.01")):
            skipped[sym] = "budget cap reached"
            continue
        try:
            r = s.call("pre_trade_check",
                       {"symbol": sym, "size_usd": TRADE_SIZE_USD, "side": "long"})
            verdicts[sym] = r.data
            log(f"bought verdict {sym}: {r.data.get('verdict')} | tx {r.tx}")
        except (PaymentFailed, RefundPending) as e:
            log(f"paid verdict {sym} failed: {e}")
            skipped[sym] = "payment failed"

    # 4. PUBLISH — note + structured findings + receipt
    regime = regime_line(_last("fear_greed_index"), _last("funding_rates"))
    context = context_line(_last("crypto_news"), _last("gas_tracker"), _last("defi_tvl"))
    findings = build_findings(spec["kind"], intel_calls, verdicts)
    if spec["kind"] == "pre_trade":
        note = compose_note(run_at, regime, verdicts, skipped, context=context)
    else:
        note = f"AgentPay flagship — {run_at}\nGoal: {spec['goal_text']}\nRegime: {regime}"
        if context:
            note += f"\nContext: {context}"
    receipt = s.spending_summary()
    free_intel = {
        "tools": free_used,
        "news": news_summary(_last("crypto_news")),
        "gas_gwei": (_last("gas_tracker") or {}).get("standard_gwei"),
        "defi_tvl_usd": _tvl_total(_last("defi_tvl")),
    }
    print("\n" + note + "\n", flush=True)
    print("FLAGSHIP_NOTE " + json.dumps({
        "run_at": run_at,
        "goal": spec["name"],
        "note": note,
        "verdicts": {k: v.get("verdict") for k, v in verdicts.items()},
        "receipt": receipt,
        "wallet": wallet.base_address,
    }), flush=True)
    log(f"run done | spent {receipt['spent']} of {receipt['budget']} "
        f"across {receipt['calls']} calls")

    # 5. PERSIST — POST the full run to the gateway so /ledger can show what the
    # agent asked, how it spent, and what came back. Best-effort: a failure here
    # never affects the exit code (any spend already settled on-chain).
    publish_run({
        "run_at": run_at,
        "run_at_iso": run_at_iso,
        "wallet": wallet.base_address,
        "max_spend": str(max_spend),
        "objective": objective,
        "plan": plan,
        "regime": regime,
        "context": context,
        "verdicts": verdicts,
        "skipped": skipped,
        "findings": findings,
        "receipt": receipt,
        "free_intel": free_intel,
        "note": note,
    })

    # A pre_trade run that bought no verdict at all is a failure worth surfacing;
    # free-only goals (regime/crowding) succeed as long as they ran.
    if spec["kind"] == "pre_trade":
        return 0 if verdicts or not paid_symbols else 1
    return 0


def run_strategy(s, spec, intel_calls, run_at, run_at_iso, wallet, max_spend, objective, plan):
    """Flagship v2 paid path (hackathon strategy goal).

    Vet the marketplace (verified_route) → consume CMC DEX data (the one leg with
    no free equivalent; honest routing keeps prices/regime free) → assemble a
    backtestable strategy spec → publish + persist. Each paid leg is gated by the
    session cap and degrades gracefully on failure.
    """
    from decimal import Decimal
    from agentpay import PaymentFailed, RefundPending

    def last(tool):
        return next((c["data"] for c in reversed(intel_calls) if c["tool"] == tool), None)

    target = spec["target_token"]
    PRICE = Decimal("0.01")

    # Honest routing: what's free vs what's worth paying for (surfaced in output).
    needs = ["spot_price", "market_regime", "dex_token_discovery", "dex_pair_liquidity"]
    routing = strategy.routing_table(needs)
    log("routing: " + "; ".join(f"{r['need']}={r['decision']}" for r in routing))

    # ── Buyer-side trust step: vet before paying a stranger ──
    vetting = None
    if not s.would_exceed(PRICE):
        try:
            vr = s.call("verified_route",
                        {"need": "dex pair liquidity data",
                         "budget_usd": float(s.remaining_usd())})
            vetting = vr.data
            rec = (vetting or {}).get("recommendation") or {}
            log(f"verified_route: {(vetting or {}).get('vetting')} | "
                f"rec {rec.get('name')} ({rec.get('payers30d')} payers) | tx {getattr(vr, 'tx', None)}")
        except (PaymentFailed, RefundPending) as e:
            log(f"verified_route failed: {e}")

    # ── Consume CMC DEX data — the paid leg with no free equivalent ──
    token = {"symbol": target, "name": target, "address": None, "network": None}
    liquidity: dict = {}
    cmc_calls: list[dict] = []
    if not s.would_exceed(PRICE):
        try:
            r = s.call(strategy.cmc_url("dex_search", {"q": target}))
            matches = strategy.parse_dex_search(r.data)
            cmc_calls.append({"endpoint": "dex_search", "matches": len(matches),
                              "tx": getattr(r, "tx", None)})
            if matches:
                token = {**token, **{k: v for k, v in matches[0].items() if v}}
            log(f"CMC dex_search '{target}': {len(matches)} matches | tx {getattr(r, 'tx', None)}")
        except (PaymentFailed, RefundPending) as e:
            log(f"CMC dex_search failed: {e}")
        except Exception as e:
            log(f"CMC dex_search error: {e}")
    if token.get("address") and not s.would_exceed(PRICE):
        try:
            r = s.call(strategy.cmc_url("dex_pairs", {"contract_address": token["address"]}))
            liquidity = strategy.parse_dex_pair(r.data)
            cmc_calls.append({"endpoint": "dex_pairs", "tx": getattr(r, "tx", None)})
            log(f"CMC dex_pairs: liquidity {liquidity.get('liquidity_usd')} | tx {getattr(r, 'tx', None)}")
        except (PaymentFailed, RefundPending) as e:
            log(f"CMC dex_pairs failed: {e}")
        except Exception as e:
            log(f"CMC dex_pairs error: {e}")

    # ── Assemble the backtestable spec (free regime + paid CMC data) ──
    regime = strategy.regime_gate((last("fear_greed_index") or {}).get("value"),
                                  _funding_bias(last("funding_rates")))
    receipt = s.spending_summary()
    spec_out = strategy.build_strategy_spec(
        token=token, regime=regime, liquidity=liquidity,
        routing=routing, receipt=receipt, run_at=run_at,
    )

    regime_text = regime_line(last("fear_greed_index"), last("funding_rates"))
    note = (f"AgentPay flagship v2 — {run_at}\nGoal: {spec['goal_text']}\n"
            f"Regime: {regime_text}\n"
            f"Strategy: {spec_out['name']} (entry_bias={regime['entry_bias']})")
    print("\n" + note + "\n", flush=True)
    print("FLAGSHIP_STRATEGY " + json.dumps({
        "run_at": run_at, "goal": spec["name"], "note": note,
        "strategy_spec": spec_out, "vetting": vetting, "cmc_calls": cmc_calls,
        "receipt": receipt, "wallet": wallet.base_address,
    }), flush=True)
    log(f"run done | spent {receipt['spent']} of {receipt['budget']} "
        f"across {receipt['calls']} calls")

    publish_run({
        "run_at": run_at, "run_at_iso": run_at_iso, "wallet": wallet.base_address,
        "max_spend": str(max_spend), "objective": objective, "plan": plan,
        "regime": regime_text, "context": "",
        "findings": {"strategy_spec": spec_out, "vetting": vetting},
        "receipt": receipt, "note": note,
    })
    return 0


def publish_run(payload: dict) -> bool:
    """POST a completed run to the gateway's flagship ingest endpoint.

    Auth is a shared secret (FLAGSHIP_INGEST_SECRET) sent as a header; the gateway
    holds the Supabase credentials and does the write, so the flagship stays a
    credential-free HTTP customer. No-op (returns False) when the secret is unset.
    Best-effort: any failure is logged and swallowed.
    """
    secret = os.environ.get("FLAGSHIP_INGEST_SECRET", "")
    if not secret:
        log("ingest skipped — FLAGSHIP_INGEST_SECRET unset")
        return False
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{GATEWAY}/v1/flagship/run",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Flagship-Secret": secret},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
        log(f"ingest {'ok' if ok else 'failed'} → /v1/flagship/run")
        return ok
    except Exception as e:
        log(f"ingest failed: {e}")
        return False


if __name__ == "__main__":
    sys.exit(main())
