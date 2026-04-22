"""
demo.py — AgentPay x402 Payment Flow Demo
==========================================

Shows how an AI agent autonomously pays for real crypto data
using the x402 protocol on Stellar — no API keys, no subscriptions.

The x402 flow in 3 steps:
  1. Agent calls a tool → gateway returns HTTP 402 (payment required)
  2. Agent pays on Stellar with USDC → gets a transaction hash
  3. Agent retries with the tx hash → gateway verifies on-chain → returns data

Run on testnet (free):
    python demo.py

Run on mainnet (real USDC):
    STELLAR_NETWORK=mainnet python demo.py
"""

import os
import sys
import time
import json
from decimal import Decimal

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

# Capture STELLAR_NETWORK before dotenv can overwrite it
_env_network = os.environ.get("STELLAR_NETWORK")

from dotenv import load_dotenv
load_dotenv(os.path.join(_here, ".env"))

from agent.wallet import AgentWallet, Session, BudgetExceeded, _fmt

# ── Config — always use live gateways, never localhost ────────────────────────
NETWORK  = _env_network or "testnet"

GATEWAYS = {
    "mainnet": "https://gateway-production-2cc2.up.railway.app",
    "testnet": "https://gateway-testnet-production.up.railway.app",
}
EXPLORERS = {
    "mainnet": "https://stellar.expert/explorer/public/tx",
    "testnet": "https://stellar.expert/explorer/testnet/tx",
}

GATEWAY  = GATEWAYS[NETWORK]
EXPLORER = EXPLORERS[NETWORK]
SECRET   = os.getenv("STELLAR_SECRET_KEY") or os.getenv("TEST_AGENT_SECRET_KEY", "")

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def _bar(spent: Decimal, budget: Decimal, width: int = 36) -> str:
    pct    = min(spent / budget, Decimal("1")) if budget > 0 else Decimal("0")
    filled = int(round(pct * width))
    if pct > Decimal("0.85"):
        color = RED
    elif pct > Decimal("0.60"):
        color = YELLOW
    else:
        color = GREEN
    bar = f"{color}{'█' * filled}{'░' * (width - filled)}{RESET}"
    return f"[{bar}] {pct*100:5.1f}%"


def banner(text: str):
    print(f"\n{CYAN}{'─' * 60}{RESET}")
    print(f"  {BOLD}{text}{RESET}")
    print(f"{CYAN}{'─' * 60}{RESET}")


def step(n: int, text: str):
    print(f"\n  {CYAN}✦ Step {n}:{RESET} {text}")


def ok(text: str):
    print(f"  {GREEN}✓{RESET} {text}")


def info(label: str, value: str):
    print(f"    {label:<18} {BOLD}{value}{RESET}")


def main():
    if not SECRET:
        print(f"\n  {RED}ERROR: No STELLAR_SECRET_KEY found in .env{RESET}")
        print(f"  Get a free testnet wallet: {GATEWAY}/faucet\n")
        sys.exit(1)

    # ── Setup ─────────────────────────────────────────────────────────────────
    wallet  = AgentWallet(secret_key=SECRET, network=NETWORK)
    balance = wallet.get_usdc_balance()

    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"  {BOLD}AgentPay — x402 Payment Flow Demo{RESET}")
    print(f"  {CYAN}Agents don't need API keys.{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    info("Network",  f"Stellar {NETWORK}")
    info("Agent",    f"{wallet.public_key[:12]}...{wallet.public_key[-6:]}")
    info("Balance",  f"{balance} USDC")
    info("Gateway",  GATEWAY)
    print(f"{BOLD}{'═' * 60}{RESET}")

    # ── Demo A: Single call — show the raw x402 flow ──────────────────────────
    banner("Demo A — Single tool call: ETH price")

    step(1, "Agent requests data → gateway returns HTTP 402")
    print()
    print("  The agent has no API key. It calls the gateway like any HTTP request.")
    print(f"  The gateway responds: {YELLOW}'402 Payment Required — send $0.001 USDC on Stellar.'{RESET}")
    time.sleep(0.6)

    with Session(wallet=wallet, gateway_url=GATEWAY, max_spend="0.05") as session:

        step(2, "Agent pays $0.001 USDC on Stellar")
        print()
        print("  The agent signs and submits a Stellar transaction autonomously.")
        print("  No human in the loop. No approval needed. Budget-capped at $0.05.")
        print()

        start = time.time()
        try:
            result = session.call("token_price", {"symbol": "ETH"})
        except BudgetExceeded as e:
            print(f"  {RED}Budget cap hit: {e}{RESET}")
            sys.exit(1)
        elapsed = time.time() - start

        log = session._call_log[-1] if session._call_log else {}
        tx  = log.get("tx_hash", "")

        step(3, "Gateway verifies on Stellar Horizon → returns real data")
        print()

        if tx:
            ok(f"Payment confirmed on Stellar {NETWORK}")
            info("Tx hash",   tx)
            info("Explorer",  f"{EXPLORER}/{tx}")
            info("Amount",    f"${log.get('amount_usdc', '0.001')} USDC")
            info("Round-trip", f"{elapsed:.2f}s")
        print()

        data = result.get("result", result)
        ok("Real-time data returned:")
        info("ETH price",    f"${data.get('price_usd', 0):,.2f}")
        info("24h change",   f"{data.get('change_24h_pct', 0):+.4f}%")
        info("Market cap",   f"${data.get('market_cap_usd', 0):,.0f}")

        # Budget bar after Demo A
        print()
        bar = _bar(session._spent, session.max_spend)
        print(f"  Budget  {bar}   spent: {session.spent()}  remaining: {session.remaining()}")

    # ── Demo B: Multi-tool agent with live budget bar ─────────────────────────
    banner("Demo B — Autonomous agent: 4 tools, $0.02 hard budget cap")

    print()
    print("  The agent plans its own workflow within a hard budget cap.")
    print("  It pays per call, tracks spend in real time, and stops if the cap is hit.")
    print()

    tools = [
        ("gas_tracker",      {},                                    "Ethereum gas prices"),
        ("fear_greed_index", {},                                    "Market sentiment (0–100)"),
        ("whale_activity",   {"token": "ETH", "min_usd": 100_000}, "Large ETH transfers"),
        ("token_price",      {"symbol": "BTC"},                    "BTC price"),
    ]

    budget_b = Decimal("0.02")

    with Session(wallet=wallet, gateway_url=GATEWAY, max_spend=str(budget_b)) as session:
        for i, (tool, params, label) in enumerate(tools, 1):
            print(f"  {BOLD}[{i}/{len(tools)}]{RESET} {label}")
            try:
                r    = session.call(tool, params)
                data = r.get("result", r)
                log  = session._call_log[-1]

                if tool == "gas_tracker":
                    info("Fast gas",    f"{data.get('fast_gwei')} gwei")
                    info("Base fee",    f"{data.get('base_fee_gwei')} gwei")
                elif tool == "fear_greed_index":
                    val = data.get("value", "?")
                    cls = data.get("value_classification", "")
                    color = GREEN if int(val) > 50 else RED
                    info("Fear & Greed", f"{color}{val}/100 — {cls}{RESET}")
                elif tool == "whale_activity":
                    count = len(data.get("large_transfers", []))
                    vol   = data.get("total_volume_usd", 0)
                    info("Transfers",   f"{count} moves")
                    info("Volume",      f"${vol:,.0f}")
                elif tool == "token_price":
                    info("BTC price",   f"${data.get('price_usd', 0):,.2f}")
                    info("24h change",  f"{data.get('change_24h_pct', 0):+.4f}%")

                bar = _bar(session._spent, session.max_spend)
                print(f"\n  Budget  {bar}")
                print(f"  Paid ${log['amount_usdc']}   |   Spent: {session.spent()}   |   Remaining: {session.remaining()}\n")

            except BudgetExceeded:
                print(f"\n  {RED}✗ Budget cap reached — agent stopped autonomously.{RESET}")
                print(f"  Spent: {session.spent()} of ${budget_b}\n")
                break

        summary = session.summary()

    # ── Final summary ─────────────────────────────────────────────────────────
    banner("Summary")
    print()
    print(f"  {BOLD}{'Tool':<28} {'Cost':>8}   Stellar Tx{RESET}")
    print(f"  {'─'*28} {'─'*8}   {'─'*20}")
    for entry in summary["breakdown"]:
        tx = (entry.get("tx_hash") or "")[:22]
        print(f"  {entry['tool']:<28} {_fmt(entry['amount_usdc']):>8}   {tx}...")

    print()
    print(f"  {BOLD}Total spent:  {summary['spent_fmt']}{RESET}")
    print(f"  Tools called: {summary['calls']}")
    print()
    print(f"  {GREEN}Every payment is a real on-chain Stellar transaction.{RESET}")
    print(f"  No API key was used. No subscription was charged.")
    print(f"  The agent paid for exactly what it needed — nothing more.")
    print()
    info("Gateway",  GATEWAY)
    info("MCP",      "npx @romudille/agentpay-mcp")
    info("Faucet",   f"{GATEWAYS['testnet']}/faucet")
    print(f"\n{BOLD}{'═' * 60}{RESET}\n")


if __name__ == "__main__":
    main()
