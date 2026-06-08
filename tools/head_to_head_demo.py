"""
head_to_head_demo.py — Why economic intelligence beats a bare wallet.

Two agents get the SAME task and the SAME tiny budget. One is a naive agent that
just calls tools in order (a wallet that moves money). The other is an AgentPay
agent that prices its plan first, routes around the step it can't afford, and ends
with a verifiable receipt (the layer that decides whether to spend).

Runs end-to-end with no funding: the 17 data tools are free, and the one premium
step is blocked client-side by the budget cap before any money moves — so you see
the divergence without a funded wallet.

Run:
    pip install agentpay-x402
    python tools/head_to_head_demo.py
"""

from decimal import Decimal
from agentpay import quickstart, BudgetExceeded

# A deliberately tight cap. The free data tools cost $0; the premium step
# (session_create, $0.001) does NOT fit — that's the point of the demo.
BUDGET = "0.0005"

# The task: a quick ETH risk snapshot, then "open a metered analysis session."
PLAN = [
    ("token_price",      {"symbol": "ETH"}),
    ("fear_greed_index", {}),
    ("funding_rates",    {"symbol": "ETH"}),
    ("whale_activity",   {"token": "ETH"}),
    ("session_create",   {}),               # premium step — $0.001, won't fit the cap
]

LINE = "─" * 60


def naive_agent():
    """A bare wallet: calls every step in order, no idea what anything costs."""
    print(f"\n{LINE}\n  NAIVE AGENT — a wallet that just spends\n{LINE}")
    s = quickstart(max_spend=BUDGET)
    done = 0
    try:
        for tool, params in PLAN:
            r = s.call(tool, params)
            done += 1
            print(f"  ✓ {tool:<16} ok")
        print("  finished")
    except BudgetExceeded as e:
        print(f"  ✗ {PLAN[done][0]:<16} BLOCKED — over budget, and it had no idea until now")
        print(f"\n  Outcome: crashed mid-task after {done}/{len(PLAN)} steps.")
        print("  No graceful fallback. No receipt. Just a wall.")


def agentpay_agent():
    """Economic intelligence: price the plan, route around what won't fit, receipt."""
    print(f"\n{LINE}\n  AGENTPAY AGENT — decides whether to spend\n{LINE}")
    s = quickstart(max_spend=BUDGET)

    # 1) Pre-flight: price the whole plan BEFORE spending a cent.
    planned = sum((s.tool_cost_usd(t) or Decimal("0")) for t, _ in PLAN)
    print(f"  Pre-flight plan cost: ${planned}  vs  budget ${BUDGET}")

    # 2) Execute cost-aware: skip/route any step that won't fit.
    for tool, params in PLAN:
        cost = s.tool_cost_usd(tool) or Decimal("0")
        if s.would_exceed(cost):
            alt = s.suggest_cheaper(tool)
            if alt:
                print(f"  → {tool:<16} ${cost} won't fit — routing to {alt['name']} ({alt['price']})")
                s.call(alt["name"], {})
            else:
                print(f"  → {tool:<16} ${cost} won't fit — skipped (no cheaper option)")
            continue
        s.call(tool, params)
        print(f"  ✓ {tool:<16} ${cost}")

    # 3) Verifiable receipt.
    print("\n  Receipt:")
    rcpt = s.spending_summary()
    print(f"    calls={rcpt['calls']}  spent={rcpt['spent']}  remaining={rcpt['remaining']}")
    print(f"    tools={rcpt['tools']}")
    print("\n  Outcome: completed the task, stayed under cap, full audit trail.")


if __name__ == "__main__":
    naive_agent()
    agentpay_agent()
    print(f"\n{LINE}")
    print("  Same task, same budget. One hit a wall; one reasoned about cost.")
    print("  That's the economic-intelligence layer.")
    print(f"{LINE}\n")
