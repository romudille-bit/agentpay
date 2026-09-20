#!/usr/bin/env python3
"""
tools/probe_agents_trust.py — buy Agentscan's (agents-trust.com) ranked
endpoint recommendations for two needs, $0.01 each, USDC on Base.

Why: as of 2026-09-20 they ship and CHARGE for the thing verified_route does —
GET https://api.agents-trust.com/recommend?need=... over x402. Their free
Discover demo answers only three canned needs (weather, crypto prices, web
search) and masks hostnames for anything else, so the only way to see whether
AgentPay is in their index, and how their ranking treats a direct substitute
for their own product, is to pay.

Two needs, deliberately different questions:
  1. a need OUR tools serve  → are we indexed, and how do we rank?
  2. a need VERIFIED_ROUTE serves → do they surface us on our own terrain?

Their 402 carries no `bazaar` extension, so the SDK could not infer that the
resource is GET-served and would have retried the paid call with POST (405 —
funds moved, no data). Fixed in agentpay/_wallet.py: the paid retry now
defaults to whichever method actually produced the 402.

The full URL (query included) is passed as the tool name with empty params, so
the URL we sign is byte-identical to the one we retry — their challenge binds
payment to a resourceHash of the exact request URL.

Run:
    $HOME/fx/bin/python tools/probe_agents_trust.py
"""
import json
import os
import sys
from urllib.parse import quote_plus

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    print("✗ No funded Base key in .env.")
    sys.exit(1)

stellar = (os.environ.get("FLAGSHIP_STELLAR_SECRET")
           or os.environ.get("AGENT_STELLAR_KEY_TEST") or "").strip()
if not stellar:
    from stellar_sdk import Keypair
    stellar = Keypair.random().secret

wallet = AgentWallet(secret_key=stellar, network="mainnet", base_key=base)
print(f"Base payer : {wallet.base_address}")

API = "https://api.agents-trust.com/recommend"
NEEDS = {
    "our_terrain": "pre-trade risk check before an agent trades crypto",
    "their_terrain": "find a trustworthy x402 endpoint that actually delivers",
}
URLS = {k: f"{API}?need={quote_plus(v)}" for k, v in NEEDS.items()}

s = Session(wallet=wallet, gateway_url="https://agentpay.tools", max_spend="0.05",
            max_per_tool={u: "0.02" for u in URLS.values()})

out = {}
for label, url in URLS.items():
    print(f"\n═══ {label} ($0.01) — need: {NEEDS[label]}")
    print(f"    {url}")
    try:
        r = s.call(url, {}, chain="base")
        data = r if isinstance(r, dict) else getattr(r, "data", r)
        out[label] = {"need": NEEDS[label], "url": url, "response": data}
        print(json.dumps(data, indent=2, default=str)[:6000])
    except PaymentFailed as e:
        print(f"✗ payment failed: {e}")
        out[label] = {"need": NEEDS[label], "url": url, "error": str(e)}
    except Exception as e:
        print(f"✗ {type(e).__name__}: {e}")
        out[label] = {"need": NEEDS[label], "url": url, "error": f"{type(e).__name__}: {e}"}

print(f"\nSession spent: {s.spent()}")
dest = os.path.join(ROOT, "agents_trust_recommend.json")
with open(dest, "w") as f:
    json.dump(out, f, indent=2, default=str)
print(f"Saved to {dest}")
