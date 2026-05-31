#!/usr/bin/env python3
"""
AgentPay — one job, many chains, one budget, one receipt
─────────────────────────────────────────────────────────
THE JOB: "Should I open a short on ETH right now?"

The agent gathers the data it needs from the best tool for each signal — and
those tools live on different chains and are found in different registries. It
discovers them (naming the registry), pays each on its own network, never
breaches one budget, reasons, and returns a go/no-go with a single receipt
spanning every chain.

Registries searched (explicit):
  • Coinbase x402 Bazaar   — Base-native tool discovery
  • 402index.io            — cross-protocol / cross-chain directory
                             (where AgentPay itself is indexed — Bazaar is Base-only)

Settlement is REAL on-chain where a wallet is funded; otherwise the leg is
clearly tagged SIM with the reason. We never fake on-chain proof.

Setup (.env): TEST_AGENT_SECRET_KEY (funded Stellar mainnet USDC + trustline),
              AGENT_BASE_KEY_TEST (funded Base USDC).
Run:  python3 tools/multichain_demo.py   [budget]
"""

import base64
import json
import os
import sys
import time
from decimal import Decimal

import requests


# ── .env loader ────────────────────────────────────────────────────────────────
def _load_dotenv():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(os.path.dirname(here), ".env")
    try:
        with open(p) as fh:
            for raw in fh:
                s = raw.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

_load_dotenv()

# ── Visuals ────────────────────────────────────────────────────────────────────
TEAL="\033[96m"; GREEN="\033[92m"; AMBER="\033[93m"; RED="\033[91m"; MAG="\033[95m"
DIM="\033[2m"; BOLD="\033[1m"; RESET="\033[0m"
def hr(c="─", w=68): print(f"{DIM}{c*w}{RESET}")
def line(): print()
def hdr(t): line(); hr(); print(f"  {TEAL}{BOLD}{t}{RESET}"); hr()
def row(l, v, c=BOLD): print(f"    {DIM}{l:<22}{RESET}{c}{v}{RESET}")
def tick(m): print(f"  {GREEN}✓{RESET}  {m}")
def wait(m): print(f"  {DIM}…{RESET}  {m}", flush=True)
def warn(m): print(f"  {AMBER}⚠{RESET}  {m}")

def _pick_gateway():
    for g in [os.environ.get("DEMO_GATEWAY"), "https://agentpay.tools",
              "https://gateway-production-2cc2.up.railway.app"]:
        if not g:
            continue
        g = g.rstrip("/")
        try:
            requests.get(f"{g}/health", timeout=8); return g
        except Exception:
            continue
    return "https://agentpay.tools"

GATEWAY      = _pick_gateway()
SESSION_URL  = f"{GATEWAY}/v1/session/create"
BAZAAR_URL   = "https://api.cdp.coinbase.com/platform/v2/x402/discovery"
INDEX_URL    = "https://402index.io"
USDC_BASE    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GATEWAY_ADDR = "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7"
BASE_RPC     = "https://mainnet.base.org"
CAIP2        = "eip155:8453"

CHAIN_COLOR = {"base": TEAL, "stellar": MAG, "free": DIM}
CHAIN_TX    = {"base": "https://basescan.org/tx/",
               "stellar": "https://stellar.expert/explorer/public/tx/"}


# ── Budget (human or agent sets it; agent enforces) ────────────────────────────
from agentpay import budget_policy
_d = budget_policy(explicit=sys.argv[1] if len(sys.argv) > 1 else None,
                   env_var="DEMO_MAX_SPEND", interactive=True,
                   prompt="Max spend for this run", usdc_balance=None, default="0.02")
BUDGET = Decimal(_d.max_spend)

spent  = Decimal("0")
ledger = []   # {signal, tool, chain, registry, cost, tx, mode, why}


# ── Banner ─────────────────────────────────────────────────────────────────────
line(); hr("═")
print(f"{BOLD}{'AgentPay — one job · many chains · one budget · one receipt':^68}{RESET}")
hr("═"); line()
print('  Job:  "Should I open a short on ETH right now?"')
print("  The agent finds the best tool for each signal — across chains and")
print("  registries — pays each on its own network under one budget cap.")
line()
row("budget cap", f"${BUDGET} USDC  (via {_d.source})")
line(); time.sleep(0.8)


# ── Provision wallets ──────────────────────────────────────────────────────────
hdr("Wallets (provisioned from env)")
line()
# Prefer a funded MAINNET Stellar key; TEST_AGENT_SECRET_KEY in .env is testnet.
stellar_secret = (os.environ.get("AGENT_STELLAR_KEY_TEST")
                  or os.environ.get("STELLAR_MAINNET_SECRET")
                  or os.environ.get("TEST_AGENT_SECRET_KEY", "")).strip()
base_hex       = os.environ.get("AGENT_BASE_KEY_TEST", "").strip().lower().removeprefix("0x")

try:
    from agentpay import AgentWallet, Session, BudgetExceeded
except ImportError:
    print("  Missing dep — pip install agentpay-x402"); sys.exit(1)

stellar_wallet = AgentWallet(secret_key=stellar_secret, network="mainnet") if stellar_secret else None
if stellar_wallet:
    bal = stellar_wallet.get_usdc_balance()
    row("Stellar agent", f"{stellar_wallet.public_key[:12]}…  USDC: {bal}")
else:
    warn("TEST_AGENT_SECRET_KEY not set — Stellar leg will be simulated")

base_addr = None
if len(base_hex) == 64:
    from eth_account import Account
    base_addr = Account.from_key("0x" + base_hex).address
    try:
        cd = "0x70a08231" + base_addr.removeprefix("0x").lower().zfill(64)
        r = requests.post(BASE_RPC, json={"jsonrpc":"2.0","method":"eth_call",
            "params":[{"to":USDC_BASE,"data":cd},"latest"],"id":1}, timeout=12).json()
        bb = int(r["result"], 16) / 1_000_000
        row("Base agent", f"{base_addr[:12]}…  USDC: ${bb:,.4f}")
    except Exception:
        row("Base agent", f"{base_addr[:12]}…")
else:
    warn("AGENT_BASE_KEY_TEST not set — Base leg will be simulated")

# One AgentWallet that holds the Base key, for free tools + the Base leg.
base_wallet = None
if base_addr:
    from stellar_sdk import Keypair
    _stell = stellar_secret or Keypair.random().secret   # ephemeral if no Stellar key
    try:
        base_wallet = AgentWallet(secret_key=_stell, network="mainnet", base_key="0x" + base_hex)
    except TypeError:
        os.environ.setdefault("BASE_AGENT_KEY", "0x" + base_hex)
        base_wallet = AgentWallet(secret_key=_stell, network="mainnet")
line(); time.sleep(0.8)


# ── Discover sources across registries (explicit) ──────────────────────────────
hdr("Discover data sources — naming each registry")
line()

def bazaar_search(q, limit=20):
    try:
        r = requests.get(f"{BAZAAR_URL}/search", params={"query": q, "network": CAIP2, "limit": limit}, timeout=12)
        return r.json().get("resources", []) if r.status_code == 200 else []
    except Exception:
        return []

def index402_search(q, limit=10):
    try:
        r = requests.get(f"{INDEX_URL}/api/v1/services", params={"q": q, "limit": limit},
                         headers={"User-Agent": "agentpay-demo/1.0"}, timeout=12)
        d = r.json()
        return d.get("services") or d.get("data") or d.get("items") or []
    except Exception:
        return []

def _callable(u):
    return isinstance(u, str) and u.startswith("http") and "/:" not in u and "{" not in u

# Base data source via Coinbase Bazaar
wait(f"Coinbase x402 Bazaar (Base) → searching 'crypto market data'…")
time.sleep(0.3)
base_tool = None
for res in bazaar_search("crypto data") + bazaar_search("market"):
    u = res.get("resource", "")
    if not _callable(u) or "agentpay" in u or GATEWAY_ADDR.lower() in json.dumps(res).lower():
        continue
    price = None
    for a in res.get("accepts", []):
        if a.get("network") == CAIP2:
            try: price = int(a.get("amount", 0)) / 1_000_000
            except Exception: pass
    if price is not None and 0 < price <= float(BUDGET):
        base_tool = {"resource": u, "price": Decimal(str(price)), "desc": res.get("description","")[:60]}
        break
if base_tool:
    tick(f"Bazaar → {base_tool['resource'].split('//')[-1][:40]}  (${base_tool['price']}, Base)")
else:
    warn("Bazaar → no cleanly-callable paid Base tool right now (Base leg will simulate)")

# Cross-chain directory via 402index (where AgentPay/Stellar are indexed)
wait(f"402index.io (cross-chain) → searching 'crypto' …")
time.sleep(0.3)
idx = index402_search("agentpay crypto")
stellar_listings = [s for s in idx if "stellar" in str(s.get("payment_network","")).lower()
                    or "agentpay.tools" in str(s.get("url",""))]
if stellar_listings:
    tick(f"402index → {len(stellar_listings)} AgentPay/Stellar-side listing(s) (Bazaar can't see these)")
    row("e.g.", (stellar_listings[0].get("name") or "?")[:42])
else:
    warn("402index → query returned no Stellar-side rows this run")
print(f"  {DIM}Stellar x402 discovery routes through 402index — the Bazaar is Base-only.")
print(f"  This is the gap AgentPay fills: one discovery+pay layer across chains.{RESET}")
line(); time.sleep(1)


# ── Helpers: per-chain real settlement (graceful sim fallback) ─────────────────
def _spend_ok(cost: Decimal) -> bool:
    return (spent + cost) <= BUDGET

# Reuse one Session for free + Base calls (no context manager → no noisy
# auto-printed summary between calls).
base_session = Session(base_wallet, gateway_url=GATEWAY, max_spend=str(BUDGET)) if base_wallet else None

def pay_base_external(url, cost):
    """Real Base payment of a third-party x402 tool via the SDK (off-chain EIP-3009)."""
    if not base_session:
        return None, "SIM", "no Base wallet"
    try:
        return base_session.call(url, {}), "REAL", ""
    except Exception as e:
        return None, "SIM", f"{type(e).__name__}: {str(e)[:50]}"

def pay_stellar_session(cost):
    """Real Stellar payment of session_create, done manually because AgentPay's
    native 402 uses `payment_options.stellar` (not the x402-v2 `accepts[]` the
    SDK's external-URL path expects): 402 → wallet.pay on Stellar → retry with
    the X-Payment proof, verified by the gateway on Horizon."""
    if not stellar_wallet:
        return None, "SIM", "no Stellar wallet"
    try:
        body = {"max_spend": str(BUDGET), "agent_address": stellar_wallet.public_key, "label": "multichain"}
        r = requests.post(SESSION_URL, json=body, timeout=15)
        if r.status_code != 402:
            return None, "SIM", f"expected 402, got {r.status_code}"
        ch = r.json()
        st = (ch.get("payment_options") or {}).get("stellar") or {}
        payment_id = st.get("payment_id") or ch.get("payment_id")
        pay_to     = st.get("pay_to") or ch.get("pay_to")
        amount     = st.get("amount_usdc") or ch.get("amount_usdc")
        if not (payment_id and pay_to and amount):
            return None, "SIM", "402 had no Stellar option"
        pay = stellar_wallet.pay(destination=pay_to, amount_usdc=str(amount), memo=payment_id[:28])
        if not pay.get("success"):
            return None, "SIM", f"stellar: {pay.get('reason','pay failed')[:40]}"
        tx = pay["tx_hash"]
        proof = f"tx_hash={tx},from={stellar_wallet.public_key},id={payment_id}"
        retry = requests.post(SESSION_URL, json=body,
                              headers={"X-Payment": proof, "X-Agent-Address": stellar_wallet.public_key}, timeout=40)
        if retry.status_code != 200:
            return None, "SIM", f"verify failed ({retry.status_code})"
        return retry.json(), "REAL", ""
    except Exception as e:
        return None, "SIM", f"{type(e).__name__}: {str(e)[:50]}"

def free_tool(name):
    """Real $0 AgentPay tool via the shared Session (settles $0)."""
    s = base_session or (Session(stellar_wallet, gateway_url=GATEWAY, max_spend=str(BUDGET)) if stellar_wallet else None)
    if not s:
        return None
    try:
        return s.call(name, {})["result"]
    except Exception:
        return None


# ── Execute the plan under one budget ──────────────────────────────────────────
hdr("Gather signals across chains — one budget enforced on every call")
line()

def record(signal, tool, chain, registry, cost, tx, mode, why=""):
    global spent
    if mode == "REAL":
        spent += cost
    ledger.append({"signal": signal, "tool": tool, "chain": chain, "registry": registry,
                   "cost": cost if mode != "FREE" else Decimal("0"), "tx": tx, "mode": mode, "why": why})

signals = {}   # raw tool responses, fed to the decision below

def _first_num(d, keys):
    """Best-effort: pull the first numeric value found under any of `keys`,
    descending into a list of dicts if needed. Tool shapes vary; stay defensive."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            for kk in keys:
                if isinstance(v[0].get(kk), (int, float)):
                    return float(v[0][kk])
    return None

def _funding_str(r):
    fr = _first_num(r, ["avg_funding_rate", "funding_rate", "rate", "fundingRate"])
    return f"{fr*100:+.4f}%/8h" if fr is not None else "funding pulled"

def _whale_str(r):
    v = _first_num(r, ["total_volume_usd", "net_flow_usd", "volume_usd"])
    return f"${v:,.0f} flow" if v is not None else "flows pulled"

# 1) Free AgentPay signals (real $0)
for sig, name, render in [
    ("ETH price + macro", "market_snapshot",  lambda r: f"ETH ${r.get('eth_price_usd',0):,.0f}"),
    ("market sentiment",  "fear_greed_index", lambda r: f"{r.get('value','?')}/100 {r.get('value_classification','')}"),
    ("perp funding",      "funding_rates",    _funding_str),
    ("whale flows",       "whale_activity",   _whale_str),
]:
    wait(f"{sig}  {DIM}(AgentPay · free · 402index){RESET}")
    time.sleep(0.25)
    data = free_tool(name)
    if data is not None:
        signals[name] = data
        tick(f"{sig}: {render(data) if isinstance(data, dict) else 'ok'}  →  $0")
        record(sig, name, "free", "402index", Decimal("0"), "", "FREE")
    else:
        warn(f"{sig}: {name} unavailable")
        record(sig, name, "free", "402index", Decimal("0"), "", "FREE", "unavailable")
    line()

# 2) Stellar leg — open AgentPay session paid ON STELLAR (home chain)
sig = "session anchor (Stellar)"
cost = Decimal("0.001")
if _spend_ok(cost):
    wait(f"{sig}  {MAG}(AgentPay session · pay on Stellar · 402index){RESET}")
    time.sleep(0.3)
    res, mode, why = pay_stellar_session(cost)
    tx = ""
    if mode == "REAL" and isinstance(res, dict):
        tx = (res.get("receipt", {}) or {}).get("tx_hash", "") or res.get("session_id", "")
    record(sig, "session_create", "stellar", "402index", cost, tx, mode, why)
    tag = f"{GREEN}REAL{RESET}" if mode == "REAL" else f"{AMBER}SIM{RESET}"
    (tick if mode == "REAL" else warn)(f"{sig}  →  ${cost}  [{tag}]  {DIM}{why}{RESET}".rstrip())
    if tx:
        print(f"      {DIM}{CHAIN_TX['stellar']}{tx[:24]}…{RESET}")
    print(f"      {DIM}spent ${spent} of ${BUDGET}{RESET}")
else:
    warn(f"{sig}: would exceed budget — refused")
line()

# 3) Base leg — premium data point from a Bazaar tool, paid ON BASE
if base_tool:
    sig = "premium market depth (Base)"
    cost = base_tool["price"]
    if _spend_ok(cost):
        wait(f"{sig}  {TEAL}(3rd-party · pay on Base · Bazaar){RESET}")
        time.sleep(0.3)
        res, mode, why = pay_base_external(base_tool["resource"], cost)
        tx = ""
        if mode == "REAL" and isinstance(res, dict):
            tx = ((res.get("payment") or {}).get("tx_hash")) or ""
        record(sig, base_tool["resource"].split("//")[-1][:28], "base", "Bazaar", cost, tx, mode, why)
        tag = f"{GREEN}REAL{RESET}" if mode == "REAL" else f"{AMBER}SIM{RESET}"
        (tick if mode == "REAL" else warn)(f"{sig}  →  ${cost}  [{tag}]  {DIM}{why}{RESET}".rstrip())
        if tx:
            print(f"      {DIM}{CHAIN_TX['base']}{tx[:24]}…{RESET}")
        print(f"      {DIM}spent ${spent} of ${BUDGET}{RESET}")
    else:
        warn(f"{sig}: ${cost} would exceed budget — refused, no spend")
        record(sig, "base_tool", "base", "Bazaar", cost, "", "REFUSED")
line(); time.sleep(0.6)


# ── Decision (illustrative synthesis) ──────────────────────────────────────────
hdr("Decision — derived from the signals it just bought")
line()

fg_raw  = signals.get("fear_greed_index", {})
fg      = None
try: fg = int(fg_raw.get("value"))
except Exception: pass
fund    = _first_num(signals.get("funding_rates", {}), ["avg_funding_rate", "funding_rate", "rate", "fundingRate"])

# Simple, transparent rule — shows the agent reasoning over what it paid for.
reasons = []
verdict = "HOLD"
if fg is not None:
    reasons.append(f"sentiment {fg}/100 ({fg_raw.get('value_classification','')})")
    if fg <= 25:
        reasons.append("crowd already fearful → contrarian risk to a fresh short")
if fund is not None:
    reasons.append(f"funding {fund*100:+.4f}%/8h")
    if fund > 0.0005 and (fg is None or fg >= 45):
        verdict = "SHORT-bias"; reasons.append("crowded longs paying to hold")
if fg is not None and fg <= 20 and (fund is None or fund <= 0):
    verdict = "AVOID short"

print(f"  {DIM}Inputs (all bought above):{RESET}")
for r in reasons:
    print(f"    {DIM}• {r}{RESET}")
line()
color = {"HOLD": AMBER, "SHORT-bias": GREEN, "AVOID short": RED}.get(verdict, BOLD)
print(f"  {GREEN}✓{RESET}  Recommendation: {color}{BOLD}{verdict}{RESET}")
print(f"  {DIM}(Illustrative rule — the demo's point is the economic plumbing, not alpha.){RESET}")
line(); time.sleep(0.6)


# ── Unified receipt across chains + registries ─────────────────────────────────
hdr("Unified receipt — one budget, every chain, every registry")
line()
print(f"  {DIM}{'signal':<26}{'chain':<9}{'registry':<10}{'cost':>8}  settle{RESET}")
hr(" ", 62)
by_chain = {}
for e in ledger:
    cc = CHAIN_COLOR.get(e["chain"], DIM)
    cost = "Free" if e["mode"] == "FREE" else (f"${e['cost']}" if e["mode"] != "REFUSED" else "—")
    mode = {"REAL": f"{GREEN}real{RESET}", "SIM": f"{AMBER}sim{RESET}",
            "FREE": f"{DIM}free{RESET}", "REFUSED": f"{RED}refused{RESET}"}[e["mode"]]
    mk = (GREEN+"✓"+RESET) if e["mode"] != "REFUSED" else (RED+"✗"+RESET)
    print(f"  {mk} {e['signal']:<24}{cc}{e['chain']:<9}{RESET}{DIM}{e['registry']:<10}{cost:>8}{RESET}  {mode}")
    if e["mode"] == "REAL":
        by_chain[e["chain"]] = by_chain.get(e["chain"], Decimal("0")) + e["cost"]
line(); hr(" ", 62)
for ch, amt in by_chain.items():
    row(f"{ch} subtotal", f"${amt}")
row("TOTAL spent", f"${spent}")
row("budget cap", f"${BUDGET}")
row("remaining", f"${max(BUDGET - spent, Decimal('0'))}")
hr()
line()
real = sum(1 for e in ledger if e["mode"] == "REAL")
chains = len({e["chain"] for e in ledger if e["chain"] != "free" and e["mode"] == "REAL"})
regs = len({e["registry"] for e in ledger})
print(f"  {BOLD}One agent. One ${BUDGET} budget. {chains} chain(s) settled, {regs} registries searched, one receipt.{RESET}")
print(f"  {DIM}AgentPay made the chain invisible to the agent — and unified discovery the Bazaar can't.{RESET}")
line()
print(f"  {DIM}pip install agentpay-x402 · agentpay.tools{RESET}")
line(); hr("═"); line()
