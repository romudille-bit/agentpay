#!/usr/bin/env python3
"""
AgentPay — the autonomous agent economy, in two acts
─────────────────────────────────────────────────────
ACT 1 (always runs) — the test that matters:
  An agent registers, gets a wallet, and uses free tools — in three API calls,
  with ZERO human involvement and ZERO funding.

      POST /v1/agent/register      → wallet + session_token   (call 1)
      GET  /tools                  → discover tools           (call 2)
      POST /tools/{name}/call      → use a tool + get receipt (call 3)

ACT 2 (optional) — when the agent needs to pay:
  With a funded Base wallet (AGENT_BASE_KEY_TEST), the same agent opens a
  budget-capped session, pays AgentPay + a third-party x402 tool on Base, and
  the budget cap refuses anything over the limit. One receipt, every provider.

Run:
  pip install requests eth-account "x402[evm]" agentpay-x402
  python3 tools/demo.py              # Act 1 (+ Act 2 if AGENT_BASE_KEY_TEST set)
  python3 tools/demo.py 0.05         # set the budget cap
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
def _load_dotenv():
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    try:
        with open(env_path, "r") as fh:
            for raw in fh:
                s = raw.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

_load_dotenv()


# ── Visual helpers ─────────────────────────────────────────────────────────────
TEAL  = "\033[96m"; GREEN = "\033[92m"; AMBER = "\033[93m"; RED = "\033[91m"
DIM   = "\033[2m";  BOLD  = "\033[1m";  RESET = "\033[0m"

def hr(char="─", w=64): print(f"{DIM}{char * w}{RESET}")
def line(): print()
def hdr(title):
    line(); hr(); print(f"  {TEAL}{BOLD}{title}{RESET}"); hr()
def row(label, value, color=BOLD):
    print(f"    {DIM}{label:<26}{RESET}{color}{value}{RESET}")
def tick(msg):  print(f"  {GREEN}✓{RESET}  {msg}")
def wait(msg):  print(f"  {DIM}…{RESET}  {msg}", flush=True)
def warn(msg):  print(f"  {AMBER}⚠{RESET}  {msg}")
def block(msg): print(f"  {RED}✗{RESET}  {msg}")

# Pick a reachable gateway. DEMO_GATEWAY overrides; otherwise try the custom
# domain, then fall back to the Railway URL (which resolves independently — so a
# DNS hiccup on agentpay.tools doesn't kill the demo).
def _pick_gateway():
    candidates = [
        os.environ.get("DEMO_GATEWAY"),
        "https://agentpay.tools",
        "https://gateway-production-2cc2.up.railway.app",
    ]
    last = "https://agentpay.tools"
    for g in candidates:
        if not g:
            continue
        g = g.rstrip("/")
        last = g
        try:
            requests.get(f"{g}/health", timeout=8)
            return g
        except Exception:
            continue
    return last

GATEWAY      = _pick_gateway()
REGISTER_URL = f"{GATEWAY}/v1/agent/register"
TOOLS_URL    = f"{GATEWAY}/tools"
SESSION_URL  = f"{GATEWAY}/v1/session/create"
BAZAAR_URL   = "https://api.cdp.coinbase.com/platform/v2/x402/discovery"
USDC_BASE    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GATEWAY_ADDR = "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7"
BASE_RPC     = "https://mainnet.base.org"
CAIP2        = "eip155:8453"
AMOUNT       = 1000   # $0.001 USDC for session_create


# ── Budget cap (human or agent sets it; agent enforces it) ─────────────────────
from agentpay import budget_policy

_decision = budget_policy(
    explicit=sys.argv[1] if len(sys.argv) > 1 else None,
    env_var="DEMO_MAX_SPEND",
    interactive=True,
    prompt="Max spend for this run",
    usdc_balance=None,          # set per-act once a wallet/balance is known
    default="0.02",
)
BUDGET = _decision.max_spend


# ─────────────────────────────────────────────────────────────────────────────
#  BANNER
# ─────────────────────────────────────────────────────────────────────────────
line(); hr("═")
print(f"{BOLD}{'AgentPay':^64}{RESET}")
print(f"{DIM}{'Register → discover → use. Zero humans.':^64}{RESET}")
print(f"{TEAL}{'agentpay.tools':^64}{RESET}")
hr("═"); line()
print("  An agent mints its own wallet, discovers tools, and uses them")
print("  in three API calls — no form, no key handed over, no human.")
print("  When it needs to pay, a hard budget cap bounds every dollar.")
line(); time.sleep(1.0)


# ═════════════════════════════════════════════════════════════════════════════
#  ACT 1 — Register, discover, use (free, zero human)
# ═════════════════════════════════════════════════════════════════════════════

hdr("Act 1 · Call 1 — Register an agent (no human, no funding)")
line()
wait(f"POST {REGISTER_URL}")
time.sleep(0.3)
try:
    reg = requests.post(REGISTER_URL, json={"label": "demo-agent", "network": "stellar"}, timeout=20)
    reg.raise_for_status()
    reg = reg.json()
except Exception as e:
    block(f"register failed ({e}) — is the gateway deployed with /v1/agent/register?")
    sys.exit(1)

wallet_info   = reg.get("wallet", {})
session_token = reg.get("session_token", "")
minted_secret = wallet_info.get("secret_key", "")
free_tools    = reg.get("free_tools", [])
tick("Agent registered — wallet minted server-side, secret returned to the agent")
row("agent_id",      (reg.get("agent_id", "") or "")[:24] + "…")
row("wallet",        f"{wallet_info.get('public_key','')[:16]}…  ({wallet_info.get('network','')})")
row("session_token", (session_token or "")[:24] + "…")
row("funding needed", "none — free tools cost $0")
line(); time.sleep(1.0)

hdr("Act 1 · Call 2 — Discover tools")
line()
wait(f"GET {TOOLS_URL}")
time.sleep(0.3)
try:
    tools_list = requests.get(TOOLS_URL, timeout=15).json().get("tools", [])
except Exception:
    tools_list = []
free_count = sum(1 for t in tools_list if float(t.get("price_usdc", "0") or "0") == 0)
tick(f"{len(tools_list)} tools available — {free_count} free")
if free_tools:
    row("free (from register)", ", ".join(free_tools[:5]) + ("…" if len(free_tools) > 5 else ""))
line(); time.sleep(1.0)

hdr("Act 1 · Call 3 — Use free tools (budget-tracked, $0)")
line()
print(f"  {DIM}The agent uses the wallet it just minted. Free tools need no")
print(f"  payment, so this runs with zero funding — but every call is")
print(f"  tracked under a budget cap and lands on one receipt.{RESET}")
line()
row("budget cap", f"${BUDGET} (set via {_decision.source})")
line()

try:
    from agentpay import AgentWallet, Session, BudgetExceeded
except ImportError:
    block("Missing dep — run  pip install agentpay-x402")
    sys.exit(1)

agent_wallet = AgentWallet(secret_key=minted_secret, network="mainnet")

with Session(agent_wallet, gateway_url=GATEWAY, max_spend=BUDGET) as s:
    renderers = {
        "market_snapshot":  lambda r: row("ETH", f"${r.get('eth_price_usd', 0):,.0f}"),
        "fear_greed_index": lambda r: row("Sentiment", f"{r.get('value','?')}/100 — {r.get('value_classification','')}"),
    }
    for tool, render in renderers.items():
        wait(f"calling {tool}  {DIM}(free){RESET}")
        time.sleep(0.3)
        try:
            data = s.call(tool, {})["result"]
            tick(tool); render(data)
        except Exception as e:
            warn(f"{tool} unavailable ({e})")
        line(); time.sleep(0.3)

    summary = s.spending_summary()

hdr("Act 1 — Receipt (zero human, zero funding)")
line()
print(f"  {DIM}{'Call':<34} {'Cost':>10}{RESET}")
hr(" ", 48)
for item in summary.get("breakdown", []):
    print(f"  {GREEN}✓{RESET}  {item['tool']:<34} {DIM}{item.get('cost','$0'):>10}{RESET}")
line(); hr()
row("Calls", str(summary["calls"]))
row("Spent", summary["spent"])
row("Funding used", "$0 — no wallet top-up, no human")
hr()
line()
print(f"  {GREEN}{BOLD}The test that matters: an agent went from nothing to using")
print(f"  tools, with a receipt, in three calls and zero human steps.{RESET}")
line(); time.sleep(1.2)


# ═════════════════════════════════════════════════════════════════════════════
#  ACT 2 — When the agent needs to pay (optional; needs a funded Base wallet)
# ═════════════════════════════════════════════════════════════════════════════

funded_hex = os.environ.get("AGENT_BASE_KEY_TEST", "").strip().lower().removeprefix("0x")
if len(funded_hex) != 64:
    hdr("Act 2 — Paid multi-provider composition (skipped)")
    line()
    print("  Set AGENT_BASE_KEY_TEST to a funded Base wallet to see the agent")
    print("  open a budget-capped session and pay AgentPay + a third-party x402")
    print("  tool on Base, with the cap refusing anything over the limit.")
    line()
    sys.exit(0)

hdr("Act 2 · Pay $0.001 USDC (Base) → open AgentPay session")
line()
funded_acct = Account.from_key("0x" + funded_hex)
funded_addr = funded_acct.address
row("funded agent", funded_addr)

# Show USDC + ETH(gas) so the paid run is informed.
def base_rpc(method, params):
    r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=15)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]

usdc_balance = None
try:
    cd = "0x70a08231" + funded_addr.removeprefix("0x").lower().zfill(64)
    usdc_balance = int(base_rpc("eth_call", [{"to": USDC_BASE, "data": cd}, "latest"]), 16) / 1_000_000
    eth_balance = int(base_rpc("eth_getBalance", [funded_addr, "latest"]), 16) / 1e18
    row("USDC balance (Base)", f"${usdc_balance:,.4f}")
    row("ETH balance (gas)", f"{eth_balance:.6f} ETH")
    if eth_balance == 0:
        warn("No ETH for gas — the third-party Base call needs a few cents of ETH.")
except Exception as e:
    warn(f"Could not read balance ({e}); continuing.")
line()

try:
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.mechanisms.evm.exact.client import ExactEvmScheme
    from x402.schemas import PaymentRequirements
except ImportError:
    block('Missing dep — run  pip install "x402[evm]"')
    sys.exit(1)
scheme = ExactEvmScheme(EthAccountSigner(funded_acct))

wait("Requesting session_create (expecting HTTP 402)...")
resp = requests.post(SESSION_URL, json={"max_spend": BUDGET, "agent_address": funded_addr}, timeout=15)
if resp.status_code != 402:
    block(f"Expected 402, got {resp.status_code}"); sys.exit(1)
tick("Gateway returned 402 — payment required")
line()

wait("Signing EIP-3009 transferWithAuthorization (off-chain, no gas)...")
requirements = PaymentRequirements(
    scheme="exact", network=CAIP2, asset=USDC_BASE, amount=str(AMOUNT),
    pay_to=GATEWAY_ADDR, max_timeout_seconds=300,
    extra={"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"},
)
payload_dict = scheme.create_payment_payload(requirements)
tick("EIP-712 signature ready — nothing broadcast yet")
line()

wait("Submitting → CDP Facilitator settles on Base...")
payment_payload = {
    "x402Version": 2, "payload": payload_dict,
    "resource": {"url": SESSION_URL, "description": "Open a budget-capped agent session.",
                 "mimeType": "application/json", "serviceName": "AgentPay",
                 "tags": ["ai-agents", "crypto", "session", "budget"]},
    "accepted": {"scheme": "exact", "network": CAIP2, "amount": str(AMOUNT), "asset": USDC_BASE,
                 "payTo": GATEWAY_ADDR, "maxTimeoutSeconds": 300, "resource": SESSION_URL,
                 "mimeType": "application/json",
                 "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"}},
}
payment_sig = base64.b64encode(json.dumps(payment_payload).encode()).decode()
paid = requests.post(SESSION_URL,
    json={"max_spend": BUDGET, "agent_address": funded_addr, "label": "act2-demo"},
    headers={"PAYMENT-SIGNATURE": payment_sig, "X-Agent-Address": funded_addr}, timeout=40)
if paid.status_code != 200:
    block(f"Payment failed ({paid.status_code}): {paid.text[:160]}"); sys.exit(1)
receipt = paid.json().get("receipt", {})
line()
tick(f"{GREEN}{BOLD}Payment verified on-chain — session open{RESET}")
row("tx_hash", (receipt.get("tx_hash", "") or "")[:24] + "…")
row("paid", f"${receipt.get('amount_usdc')} USDC (provider: AgentPay)")
print(f"  {DIM}Verify: https://basescan.org/tx/{receipt.get('tx_hash')}{RESET}")
line(); time.sleep(1.2)

# ── Discover a third-party x402 tool on Base ───────────────────────────────────
hdr("Act 2 · Discover + pay a third-party tool (one budget)")
line()

def bazaar_search(query, limit=20):
    try:
        r = requests.get(f"{BAZAAR_URL}/search", params={"query": query, "network": CAIP2, "limit": limit}, timeout=12)
        return r.json().get("resources", []) if r.status_code == 200 else []
    except Exception:
        return []

def cheapest_usd(res):
    best = None
    for a in res.get("accepts", []):
        if a.get("network") != CAIP2:
            continue
        try:
            usd = int(a.get("amount", 0)) / 1_000_000
        except (ValueError, TypeError):
            continue
        best = usd if best is None or usd < best else best
    return best

def _callable_url(u):
    return u.startswith("http") and "/:" not in u and "{" not in u

external = None
for res in bazaar_search("crypto data") + bazaar_search("market"):
    u = res.get("resource", "")
    if not _callable_url(u) or "agentpay" in u or GATEWAY_ADDR.lower() in json.dumps(res).lower():
        continue
    price = cheapest_usd(res)
    if price is not None and 0 < price <= float(BUDGET):
        external = {"resource": u, "price": price, "desc": res.get("description", "")[:70]}
        break
if external is None and os.environ.get("DEMO_EXTERNAL_URL"):
    up, _, pp = os.environ["DEMO_EXTERNAL_URL"].partition("|")
    if _callable_url(up.strip()):
        external = {"resource": up.strip(), "price": float(pp) if pp else 0.001, "desc": "pinned"}

# Paid composition + budget-bite, using the funded Base wallet via the SDK.
stellar_secret = os.environ.get("STELLAR_SECRET", "").strip()
if not stellar_secret:
    from stellar_sdk import Keypair
    stellar_secret = Keypair.random().secret
try:
    paid_wallet = AgentWallet(secret_key=stellar_secret, network="mainnet", base_key="0x" + funded_hex)
except TypeError:
    os.environ.setdefault("BASE_AGENT_KEY", "0x" + funded_hex)
    paid_wallet = AgentWallet(secret_key=stellar_secret, network="mainnet")

with Session(paid_wallet, gateway_url=GATEWAY, max_spend=BUDGET) as ps:
    if external:
        tick(f"Found third-party tool: {external['resource'][:40]}… (${external['price']:.4f})")
        wait("paying off-chain (sign EIP-3009 → settle on accept)...")
        time.sleep(0.3)
        try:
            ext = ps.call(external["resource"], {})
            tick("third-party tool paid + returned data")
            row("response", (json.dumps(ext)[:60] + "…"))
        except BudgetExceeded as e:
            block(f"refused by budget: {e}")
        except Exception as e:
            warn(f"third-party tool failed ({e}) — no funds moved (off-chain flow)")
        print(f"      {DIM}spent {ps.spent()} · remaining {ps.remaining()} of ${BUDGET}{RESET}")
        line()
    else:
        warn("No cleanly-callable third-party tool found on Bazaar right now.")
        line()

    # The budget bites — deterministic, no spend.
    remaining = Decimal(ps.remaining().lstrip("$"))
    pricey = remaining + Decimal("0.01")
    wait(f"considering a ${pricey} tool with {ps.remaining()} left...")
    time.sleep(0.3)
    if ps.would_exceed(str(pricey)):
        block(f"{BOLD}BudgetExceeded{RESET} — call refused before signing")
        tick("No payment was signed. The cap held.")
    line()
    paid_summary = ps.spending_summary()

hdr("Act 2 — Unified receipt (all providers)")
line()
print(f"  {DIM}{'Call':<40} {'Cost':>10}{RESET}")
hr(" ", 54)
print(f"  {GREEN}✓{RESET}  {'session_create  (AgentPay)':<40} {DIM}{'$0.001':>10}{RESET}")
for item in paid_summary.get("breakdown", []):
    name = item["tool"]
    if name.startswith("http"):
        name = "↗ " + name.split("//", 1)[-1][:34]
    print(f"  {GREEN}✓{RESET}  {name:<40} {DIM}{item.get('cost','$0'):>10}{RESET}")
line(); hr()
total = Decimal(paid_summary["spent"].lstrip("$")) + Decimal("0.001")
row("Total this run", f"${total}")
row("Budget cap", f"${BUDGET}")
row("Remaining", paid_summary["remaining"])
hr()
line()
print(f"  {DIM}pip install agentpay-x402  ·  npx @romudille/agentpay-mcp{RESET}")
print(f"  {TEAL}agentpay.tools{RESET}")
line(); hr("═"); line()
