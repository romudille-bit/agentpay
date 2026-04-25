"""
agent/wallet.py — DEPRECATED re-export shim.

The canonical implementation lives in `agentpay/_wallet.py` and is the version
shipped to PyPI as `agentpay-x402`. This module used to be a hand-maintained
copy that gradually drifted (missing `PaymentFailed`, no `_extract_stellar_reason`
helper, dead imports of `agent.agent.AgentPayClient`).

Everything is now re-exported from `agentpay._wallet` so old imports keep
working. New code should import from `agentpay` directly:

    # Preferred (public API):
    from agentpay import AgentWallet, Session, BudgetExceeded, PaymentFailed

    # Legacy (still works):
    from agent.wallet import AgentWallet, Session, BudgetExceeded

This shim exists to avoid breaking:
  - agent/agent.py
  - agent/budget_demo.py
  - agent/week2_test.py
  - demo.py
  - gateway/main.py:933 (lazy in-function import)

It can be deleted once those callers migrate to `from agentpay import ...`.
"""

from agentpay._wallet import (  # noqa: F401  (re-exports)
    # Module-level constants
    HORIZON_TESTNET,
    HORIZON_MAINNET,
    USDC_ISSUER_TESTNET,
    USDC_ISSUER_MAINNET,
    logger,
    # Exceptions
    BudgetExceeded,
    PaymentFailed,
    # Core classes
    AgentWallet,
    Session,
    BudgetSession,
    # Helpers
    _fmt,
    _extract_stellar_reason,
)

__all__ = [
    "HORIZON_TESTNET",
    "HORIZON_MAINNET",
    "USDC_ISSUER_TESTNET",
    "USDC_ISSUER_MAINNET",
    "BudgetExceeded",
    "PaymentFailed",
    "AgentWallet",
    "Session",
    "BudgetSession",
    "_fmt",
    "_extract_stellar_reason",
]
