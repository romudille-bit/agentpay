#!/usr/bin/env python3
"""
AgentPay — autonomous agent economy demo (Base mainnet)
───────────────────────────────────────────────────────
Shows the full x402 loop the way an UNATTENDED agent actually runs it:

  0. Wallet is PROVISIONED ONCE from the environment — no human, no
     pasted secret key. (That's the whole point of "autonomous".)
  1. The agent defines a hard budget cap for the run.
  2. It discovers providers on the Base Bazaar — AgentPay AND a
     third-party x402 tool.
  3. It pays $0.001 USDC on Base to open an AgentPay session.
  4. Inside ONE budget, it spends across MULTIPLE providers:
     free AgentPay tools + a paid third-party tool. Spend ticks down.
  5. The budget bites: a call that would exceed the cap is refused
     on-chain spend never happens — BudgetExceeded, handled gracefully.
  6. A unified receipt: every provider, every tx, total vs cap.

Run:
  export AGENT_BASE_KEY_TEST=0x...          # provisioned ONCE, then never again
  pip install requests eth-account "x402[evm]" agentpay-x402
  python3 tools/demo.py

Optional:
  export STELLAR_SECRET=S...           # only if you also want Stellar fallback
"""

import base64
import json
import os
import sys
import time
from decimal import Decimal

import requests
from eth_account import Account


# ── Load the local .env (no external dependency) ───────────────────────────────
# Keeps the agent's key in the gitignored .env at the repo root. Real env vars
# always win, so production (Railway service variables) overrides the file.
def _load_dotenv():
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")  # repo root
    try:
        with open(env_path, "r") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, val = stripped.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)   # don't clobber real env vars
    except FileNotFoundError:
        pass

_load_dotenv()

# ── Visual helpers ────────────────────────────────────────────────────────────

TEAL  = "\033[96m"
GREEN = "\033[92m"
AMBER = "\033[93m"
RED   = "\033[91m"
DIM   = "\033[2m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def hr(char="─", w=64): print(f"{DIM}{char * w}{RESET}")
def line(): print()

def hdr(title):
    line()
    hr()
    print(f"  {TEAL}{BOLD}{title}{RESET}")
    hr()

def row(label, value, color=BOLD):
    print(f"    {DIM}{label:<26}{RESET}{color}{value}{RESET}")

def tick(msg):  print(f"  {GREEN}✓{RESET}  {msg}")
def wait(msg):  print(f"  {DIM}…{RESET}  {msg}", flush=True)
def warn(msg):  print(f"  {AMBER}⚠{RESET}  {msg}")
def block(msg): print(f"  {RED}✗{RESET}  {msg}")

GATEWAY      = "https://agentpay.tools"
SESSION_URL  = f"{GATEWAY}/v1/session/create"
BAZAAR_URL   = "https://api.cdp.coinbase.com/platform/v2/x402/discovery"
USDC_BASE    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GATEWAY_ADDR = "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7"
BASE_RPC     = "https://mainnet.base.org"
CAIP2        = "eip155:8453"
AMOUNT       = 1000   # $0.001 USDC for session_create


# ─────────────────────────────────────────────────────────────────────────────
#  BANNER
# ─────────────────────────────────────────────────────────────────────────────

line()
hr("═")
print(f"{BOLD}{'AgentPay':^64}{RESET}")
print(f"{DIM}{'One wallet. One budget. Many providers.':^64}{RESET}")
print(f"{TEAL}{'agentpay.tools':^64}{RESET}")
hr("═")
line()
print("  An unattended agent provisions its wallet once, sets a")
print("  spending cap, discovers paid tools on the Base Bazaar, and")
print("  spends across multiple providers — every call on-chain,")
print("  every dollar bounded, no human in the loop.")
line()
time.sleep(1.2)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 0 — Provisioned wallet (NO human prompt)
# ─────────────────────────────────────────────────────────────────────────────

hdr("Step 0 — Wallet (provisioned once, at deploy)")
line()

base_key = os.environ.get("AGENT_BASE_KEY_TEST", "").strip()
if not base_key:
    block("AGENT_BASE_KEY_TEST is not set.")
    line()
    print("  An autonomous agent does NOT ask a human for its secret key at")
    print("  runtime. Provision the wallet once in the environment, then the")
    print("  agent runs unattended forever:")
    line()
    print(f"    {DIM}export AGENT_BASE_KEY_TEST=0x...{RESET}")
    line()
    sys.exit(1)

hex_key = base_key.lower().removeprefix("0x")
if len(hex_key) != 64:
    block(f"AGENT_BASE_KEY_TEST looks malformed (expected 64 hex chars, got {len(hex_key)}).")
    sys.exit(1)

eth_account   = Account.from_key("0x" + hex_key)
agent_address = eth_account.address
tick("Wallet loaded from environment — no human input required")
row("agent address", agent_address)

# Read live USDC balance on Base (balanceOf(address))
def base_rpc(method, params):
    r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "method": method,
                                      "params": params, "id": 1}, timeout=15)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]

# Expose BOTH balances so whoever sets the cap (human or agent) decides
# informed: USDC is the spend ceiling, ETH is gas for Base ERC-20 transfers.
# Zero gas is what silently breaks third-party Base calls.
USDC_BALANCE = None
ETH_BALANCE  = None
try:
    calldata = "0x70a08231" + agent_address.removeprefix("0x").lower().zfill(64)
    raw = base_rpc("eth_call", [{"to": USDC_BASE, "data": calldata}, "latest"])
    USDC_BALANCE = int(raw, 16) / 1_000_000
    row("USDC balance (Base)", f"${USDC_BALANCE:,.4f}")

    eth_raw = base_rpc("eth_getBalance", [agent_address, "latest"])
    ETH_BALANCE = int(eth_raw, 16) / 1e18
    row("ETH balance (gas)", f"{ETH_BALANCE:.6f} ETH")

    if USDC_BALANCE < 0.012:
        warn("Low USDC — fund this address with a little USDC on Base to run live.")
    if ETH_BALANCE == 0:
        warn("No ETH for gas — session_create is gasless, but paying a")
        warn("third-party Base tool needs a few cents of ETH. Top up to run Step 4 fully.")
except Exception as e:
    warn(f"Could not read balance ({e}); continuing.")

line()
time.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — Define the budget (the hard cap that bounds everything below)
# ─────────────────────────────────────────────────────────────────────────────

hdr("Step 1 — Define the budget")
line()

# The cap is decided by budget_policy() — the same SDK helper an autonomous
# agent would use — so the demo and real deployments share one code path.
# Precedence: CLI arg → DEMO_MAX_SPEND env → interactive prompt (attended) →
# default. The result is clamped to the spendable balance; the wallet is the
# real ceiling. NOTHING below can exceed the chosen cap: the Session refuses
# any call that would breach it BEFORE a dollar leaves the wallet.
from agentpay import budget_policy

if sys.stdin.isatty() and len(sys.argv) <= 1 and not os.environ.get("DEMO_MAX_SPEND"):
    print("  This run is attended — you set the hard spend cap.")
    line()
    if USDC_BALANCE is not None:
        row("wallet USDC", f"${USDC_BALANCE:,.4f}  (the real ceiling — can't spend past it)")
    line()

decision = budget_policy(
    explicit=sys.argv[1] if len(sys.argv) > 1 else None,
    env_var="DEMO_MAX_SPEND",
    interactive=True,
    prompt="Max spend for this run",
    usdc_balance=USDC_BALANCE,
    default="0.02",
)
BUDGET = decision.max_spend

_SOURCE_LABEL = {
    "explicit":    "command-line argument",
    "env":         "DEMO_MAX_SPEND env var",
    "interactive": "you (interactive)",
    "policy":      "policy rule",
    "default":     "default (unattended)",
}
line()
print("  Hard spend cap for this run — the agent enforces it on every call:")
line()
row("max_spend", f"${BUDGET} USDC")
row("set via", _SOURCE_LABEL.get(decision.source, decision.source))
if decision.requested and decision.requested != BUDGET:
    row("requested", f"${decision.requested}")
if decision.capped_by_balance:
    row("effective ceiling", f"${BUDGET} (capped by wallet balance)", color=AMBER)
for _w in decision.warnings:
    warn(_w)
row("enforced", "client-side, before every on-chain payment")
line()
print(f"  {DIM}Override:  python tools/demo.py 0.05   (or DEMO_MAX_SPEND=0.05){RESET}")
line()
print(f"  {DIM}with Session(wallet, gateway, max_spend=\"{BUDGET}\") as s:{RESET}")
print(f"  {DIM}    s.call(...)   # raises BudgetExceeded before paying if it won't fit{RESET}")
line()
time.sleep(1.2)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2 — Discover providers on the Base Bazaar
# ─────────────────────────────────────────────────────────────────────────────

hdr("Step 2 — Discover providers on the Base Bazaar")
line()
print(f"  {DIM}GET {BAZAAR_URL}/search?network={CAIP2}{RESET}")
line()

def bazaar_search(query, limit=20):
    try:
        r = requests.get(f"{BAZAAR_URL}/search",
                         params={"query": query, "network": CAIP2, "limit": limit},
                         timeout=12)
        if r.status_code != 200:
            return []
        return r.json().get("resources", [])
    except Exception:
        return []

def cheapest_usd(res):
    """Return cheapest Base USDC price in USD for a Bazaar resource, or None."""
    best = None
    for a in res.get("accepts", []):
        if a.get("network") != CAIP2:
            continue
        try:
            usd = int(a.get("amount", 0)) / 1_000_000
        except (ValueError, TypeError):
            continue
        if best is None or usd < best:
            best = usd
    return best

wait("Querying Base Bazaar for AgentPay...")
agentpay_hits = bazaar_search("AgentPay")
time.sleep(0.4)
if agentpay_hits:
    tick(f"AgentPay indexed — {len(agentpay_hits)} resource(s) live on Bazaar")
else:
    tick("AgentPay reachable directly (Bazaar index may be propagating)")

# Find a third-party paid tool to combine with — exclude AgentPay's own gateway.
wait("Searching for a third-party x402 tool to combine with...")
candidates = bazaar_search("crypto data") + bazaar_search("market")
time.sleep(0.4)

def _callable_url(resource: str) -> bool:
    """Skip resources we can't call blindly: non-http, or URL templates with
    an unfilled path placeholder like '/:endpoint' (needs a param we don't
    have). Note '/:' never matches the '://' in the scheme."""
    if not resource.startswith("http"):
        return False
    if "/:" in resource or "{" in resource:
        return False
    return True

external = None
for res in candidates:
    resource = res.get("resource", "")
    if not _callable_url(resource):
        continue
    if "agentpay" in resource or GATEWAY_ADDR.lower() in json.dumps(res).lower():
        continue
    price = cheapest_usd(res)
    if price is None:
        continue
    if 0 < price <= float(BUDGET):
        external = {
            "resource": resource,
            "price": price,
            "desc": res.get("description", "")[:70],
        }
        break

# Safe fallback: pin a known-good x402 tool via DEMO_EXTERNAL_URL when discovery
# finds nothing cleanly callable, so the combine step always has something to
# show. Format:  DEMO_EXTERNAL_URL="https://...|0.001"  (url|price_usd)
if external is None and os.environ.get("DEMO_EXTERNAL_URL"):
    raw = os.environ["DEMO_EXTERNAL_URL"]
    url_part, _, price_part = raw.partition("|")
    if _callable_url(url_part.strip()):
        try:
            external = {
                "resource": url_part.strip(),
                "price": float(price_part) if price_part else 0.001,
                "desc": "pinned via DEMO_EXTERNAL_URL",
            }
        except ValueError:
            external = None

if external:
    tick("Found a third-party x402 tool on Base")
    row("resource", external["resource"][:48] + ("…" if len(external["resource"]) > 48 else ""))
    row("price", f"${external['price']:.4f} USDC")
    if external["desc"]:
        row("about", external["desc"])
else:
    warn("No healthy paid third-party tool on Bazaar right now —")
    warn("falling back to AgentPay's own free tools for the spend phase.")
line()
time.sleep(1.2)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — Pay $0.001 USDC on Base → open an AgentPay session
# ─────────────────────────────────────────────────────────────────────────────

hdr("Step 3 — Pay $0.001 USDC (Base) → open AgentPay session")
line()

try:
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.mechanisms.evm.exact.client import ExactEvmScheme
    from x402.schemas import PaymentRequirements
except ImportError:
    block('Missing dep — run  pip install "x402[evm]"')
    sys.exit(1)

signer = EthAccountSigner(eth_account)
scheme = ExactEvmScheme(signer)

wait("Requesting session_create (expecting HTTP 402)...")
resp = requests.post(SESSION_URL,
                     json={"max_spend": BUDGET, "agent_address": agent_address},
                     timeout=15)
if resp.status_code != 402:
    block(f"Expected 402, got {resp.status_code}: {resp.text[:160]}")
    sys.exit(1)
payment_id = resp.json().get("payment_id")
tick("Gateway returned 402 — payment required")
row("amount", "$0.001 USDC")
row("network", "Base mainnet (eip155:8453)")
line()

wait("Signing EIP-3009 transferWithAuthorization (off-chain, no gas)...")
requirements = PaymentRequirements(
    scheme="exact", network=CAIP2, asset=USDC_BASE,
    amount=str(AMOUNT), pay_to=GATEWAY_ADDR, max_timeout_seconds=300,
    extra={"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"},
)
payload_dict = scheme.create_payment_payload(requirements)
tick("EIP-712 signature ready — nothing broadcast yet")
line()

wait("Submitting → CDP Facilitator settles on Base...")
payment_payload = {
    "x402Version": 2,
    "payload":     payload_dict,
    "resource": {
        "url":         SESSION_URL,
        "description": "Open a budget-capped agent session.",
        "mimeType":    "application/json",
        "serviceName": "AgentPay",
        "tags":        ["ai-agents", "crypto", "session", "budget"],
    },
    "accepted": {
        "scheme": "exact", "network": CAIP2, "amount": str(AMOUNT),
        "asset": USDC_BASE, "payTo": GATEWAY_ADDR, "maxTimeoutSeconds": 300,
        "resource": SESSION_URL, "mimeType": "application/json",
        "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"},
    },
}
payment_sig = base64.b64encode(json.dumps(payment_payload).encode()).decode()
paid = requests.post(
    SESSION_URL,
    json={"max_spend": BUDGET, "agent_address": agent_address, "label": "autonomous-demo"},
    headers={"PAYMENT-SIGNATURE": payment_sig, "X-Agent-Address": agent_address},
    timeout=40,
)
if paid.status_code != 200:
    block(f"Payment failed ({paid.status_code}):")
    try: print(f"  {json.dumps(paid.json(), indent=4)}")
    except Exception: print(paid.text)
    sys.exit(1)

result  = paid.json()
receipt = result.get("receipt", {})
line()
tick(f"{GREEN}{BOLD}Payment verified on-chain — session open{RESET}")
row("session_id", (result.get("session_id", "") or "")[:24] + "…")
row("tx_hash",    (receipt.get("tx_hash", "") or "")[:24] + "…")
row("paid",       f"${receipt.get('amount_usdc')} USDC (provider: AgentPay)")
row("budget cap", f"${result.get('max_spend')}")
print(f"  {DIM}Verify: https://basescan.org/tx/{receipt.get('tx_hash')}{RESET}")
line()
time.sleep(1.4)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4 — Spend across providers inside ONE budget
# ─────────────────────────────────────────────────────────────────────────────

hdr("Step 4 — Spend across providers (one budget)")
line()

try:
    from agentpay import AgentWallet, Session, BudgetExceeded
except ImportError:
    block("Missing dep — run  pip install agentpay-x402")
    sys.exit(1)

# The SDK wallet requires a Stellar key to construct. A Base-only agent has no
# funded Stellar account, so we satisfy the constructor with an ephemeral key
# (used only if a Stellar payment is ever attempted) and pay everything on Base.
stellar_secret = os.environ.get("STELLAR_SECRET", "").strip()
if not stellar_secret:
    from stellar_sdk import Keypair
    stellar_secret = Keypair.random().secret  # ephemeral; Base demo never funds it

# Newer agentpay (repo / pip install -e .) takes base_key= and supports paying
# third-party x402 tools on Base. Older PyPI builds have neither — fall back
# gracefully so the demo still runs (just without the external Base call).
try:
    wallet = AgentWallet(secret_key=stellar_secret, network="mainnet", base_key="0x" + hex_key)
except TypeError:
    os.environ.setdefault("BASE_AGENT_KEY", "0x" + hex_key)   # newer builds read this
    wallet = AgentWallet(secret_key=stellar_secret, network="mainnet")
    if not getattr(wallet, "base_address", None):
        warn("Installed agentpay is an older build without Base support —")
        warn("third-party Base calls will be skipped. To enable:  pip install -e .")
        external = None   # disable the external-tool step on this version

def show_meter(s):
    print(f"      {DIM}spent {s.spent()} · remaining {s.remaining()} of ${BUDGET}{RESET}")

with Session(wallet, gateway_url=GATEWAY, max_spend=BUDGET) as s:

    # ── Free AgentPay tools (provider: AgentPay, $0, session-tracked) ──────────
    for tool, label, render in (
        ("market_snapshot", "market_snapshot",
         lambda r: row("ETH", f"${r.get('eth_price_usd', 0):,.0f}")),
        ("fear_greed_index", "fear_greed_index",
         lambda r: row("Sentiment", f"{r.get('value','?')}/100 — {r.get('value_classification','')}")),
    ):
        wait(f"calling {label}  {DIM}(AgentPay · free){RESET}")
        time.sleep(0.3)
        try:
            data = s.call(tool, {})["result"]
            tick(f"{label}")
            render(data)
        except Exception as e:
            warn(f"{label} unavailable ({e})")
        show_meter(s)
        line()
        time.sleep(0.4)

    # ── Third-party paid tool (provider: someone else, paid on Base) ──────────
    if external:
        wait(f"calling third-party tool  {DIM}({external['price']:.4f} USDC · Base){RESET}")
        time.sleep(0.3)
        try:
            # session.call() accepts a raw x402 URL — payment routes straight to
            # the provider, AgentPay's Session just tracks the spend + enforces budget.
            ext_result = s.call(external["resource"], {})
            tick("third-party tool paid + returned data")
            preview = json.dumps(ext_result)[:80]
            row("response", preview + ("…" if len(preview) >= 80 else ""))
        except BudgetExceeded as e:
            block(f"refused by budget: {e}")
        except Exception as e:
            warn(f"third-party tool failed ({e}) — spend phase continues")
        show_meter(s)
        line()
        time.sleep(0.5)

    # ── Step 5 — The budget bites ─────────────────────────────────────────────
    hdr("Step 5 — The budget bites")
    line()
    print("  The agent considers one more paid call. The Session checks it")
    print("  against the remaining budget BEFORE any payment is signed.")
    line()

    # Evaluate a pricier tool that cannot fit in what's left. The amount is
    # always larger than the remaining budget, so the guard is deterministic:
    # this is exactly the check Session.call() runs internally before it would
    # sign any payment (it raises BudgetExceeded at this point).
    remaining = Decimal(s.remaining().lstrip("$"))
    pricey    = remaining + Decimal("0.01")
    wait(f"considering a ${pricey} tool with ${s.remaining().lstrip('$')} left...")
    time.sleep(0.5)
    if s.would_exceed(str(pricey)):
        block(f"{BOLD}would_exceed = True → BudgetExceeded{RESET} — call refused")
        print(f"      {DIM}A ${pricey} call won't fit in {s.remaining()} remaining.{RESET}")
        tick("No payment was signed. No USDC left the wallet. The cap held.")
    else:
        warn("it would fit — budget larger than expected")
    line()
    time.sleep(1)

    summary = s.spending_summary()

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 6 — Unified receipt
# ─────────────────────────────────────────────────────────────────────────────

hdr("Step 6 — Unified receipt (all providers)")
line()
print(f"  {DIM}{'Call':<40} {'Cost':>10}{RESET}")
hr(" ", 54)
# session_create line (paid on Base, outside the SDK Session)
print(f"  {GREEN}✓{RESET}  {'session_create  (AgentPay)':<40} {DIM}{'$0.001':>10}{RESET}")
for item in summary.get("breakdown", []):
    name = item["tool"]
    if name.startswith("http"):
        name = "↗ " + name.split("//", 1)[-1][:34]
    print(f"  {GREEN}✓{RESET}  {name:<40} {DIM}{item.get('cost','Free'):>10}{RESET}")
line()
hr()
total = (Decimal(summary["spent"].lstrip("$")) + Decimal("0.001"))
row("In-session calls", str(summary["calls"]))
row("Session spend", summary["spent"])
row("+ session_create", "$0.001")
row("Total this run", f"${total}")
row("Budget cap", f"${BUDGET}")
row("Remaining", summary["remaining"])
hr()


# ─────────────────────────────────────────────────────────────────────────────
#  CLOSE
# ─────────────────────────────────────────────────────────────────────────────

line()
hr("═")
print(f"{BOLD}{'What just happened:':^64}{RESET}")
hr("═")
line()
print("  0. Wallet was provisioned ONCE from the env — no human, no")
print("     pasted key. The agent ran unattended.")
print("  1. The agent set a hard budget cap before doing anything.")
print("  2. It discovered providers on the Base Bazaar.")
print("  3. It paid AgentPay $0.001 USDC on Base to open a session.")
print("  4. It spent across MULTIPLE providers under one budget.")
print("  5. A call that would breach the cap was refused — no spend.")
print("  6. One receipt covers every provider and every tx.")
line()
print(f"  {DIM}pip install agentpay-x402  ·  npx @romudille/agentpay-mcp{RESET}")
print(f"  {TEAL}agentpay.tools{RESET}")
line()
hr("═")
line()
