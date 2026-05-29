"""
budget_policy — decide a Session spend cap from a clear, auditable policy.

The cap that bounds an agent run can come from several places. In an
unattended deployment it's set programmatically; in an attended one a human
sets it; and a self-governing agent can derive it from a rule (e.g. "never
risk more than 25% of the wallet in one run"). This helper makes that
decision explicit, with a fixed precedence, and returns *why* — so the choice
is logged and reviewable rather than buried in scattered `if` branches.

Precedence (first match wins):
    1. explicit            — a value you pass in directly
    2. env var             — os.environ[env_var]
    3. interactive prompt  — only when attended (a TTY) and `interactive=True`
    4. policy rule         — pct_of_balance, clamped by min/max
    5. default             — fallback constant

Whatever the source, the result is clamped to what's actually spendable:
the wallet's USDC balance minus an optional reserve. The balance is the real
hard ceiling — no policy can spend money that isn't there.

Example (autonomous, rule-based):
    from agentpay import AgentWallet, Session, budget_policy

    wallet  = AgentWallet(secret_key=SECRET, network="mainnet")
    balance = float(wallet.get_usdc_balance())

    decision = budget_policy(
        usdc_balance=balance,
        pct_of_balance=0.25,        # risk at most 25% of the wallet per run
        max_cap="0.50",            # ...but never more than $0.50
        min_cap="0.01",
        env_var="AGENT_MAX_SPEND",  # an operator override, if set
    )
    print(decision.explain())

    with Session(wallet, max_spend=decision.max_spend) as s:
        ...

Example (attended, human decides, with approval gate):
    decision = budget_policy(
        usdc_balance=balance,
        interactive=True,
        approve_above="1.00",       # caps over $1 require explicit confirmation
    )
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional


def _to_decimal(value) -> Optional[Decimal]:
    """Parse to a positive Decimal, or None if invalid / non-positive."""
    if value is None:
        return None
    try:
        d = Decimal(str(value).strip().lstrip("$"))
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


@dataclass
class BudgetDecision:
    """The outcome of budget_policy(): the cap, where it came from, and why."""
    max_spend: str                     # the cap to hand to Session(max_spend=)
    source: str                        # "explicit" | "env" | "interactive" | "policy" | "default"
    requested: Optional[str] = None    # what the source asked for, pre-clamp
    capped_by_balance: bool = False    # True if the wallet balance lowered it
    needs_approval: bool = False       # True if it crossed approve_above and wasn't confirmed
    warnings: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return not self.needs_approval

    def explain(self) -> str:
        parts = [f"max_spend=${self.max_spend} (via {self.source}"]
        if self.requested and self.requested != self.max_spend:
            parts.append(f", requested ${self.requested}")
        parts.append(")")
        line = "".join(parts)
        if self.capped_by_balance:
            line += " — clamped to wallet balance"
        if self.needs_approval:
            line += " — NEEDS APPROVAL"
        for w in self.warnings:
            line += f"\n  ! {w}"
        return line


def budget_policy(
    *,
    explicit=None,
    env_var: Optional[str] = None,
    interactive: bool = False,
    prompt: str = "Max spend for this run",
    usdc_balance: Optional[float] = None,
    reserve: str = "0",
    pct_of_balance: Optional[float] = None,
    min_cap: Optional[str] = None,
    max_cap: Optional[str] = None,
    default: str = "0.02",
    approve_above: Optional[str] = None,
    approver: Optional[Callable[[Decimal], bool]] = None,
) -> BudgetDecision:
    """
    Decide a spend cap. See module docstring for precedence and examples.

    Args:
        explicit:        A cap value to use directly (highest precedence).
        env_var:         Name of an env var to read a cap from, if `explicit`
                         is not given.
        interactive:     If True AND stdin is a TTY, prompt the human (used
                         only when explicit/env produced nothing).
        prompt:          Text shown at the interactive prompt.
        usdc_balance:    Wallet USDC balance. Used both for pct_of_balance and
                         to clamp the final cap (you can't spend what you don't
                         have). If None, no balance clamp is applied.
        reserve:         USDC to hold back from the balance ceiling (e.g. to
                         leave room for fees). Subtracted before clamping.
        pct_of_balance:  Rule-based fraction of `usdc_balance` (0–1) to use
                         when no explicit/env/interactive value is available.
        min_cap/max_cap: Clamp the policy-derived (and final) cap to this range.
        default:         Fallback when nothing else yields a value.
        approve_above:   If the chosen cap exceeds this, mark needs_approval
                         (or call `approver` to decide).
        approver:        Optional callback(amount: Decimal) -> bool to approve
                         a cap above `approve_above`. Returns True to allow.

    Returns:
        BudgetDecision — `.max_spend` is the string to pass to Session, and
        `.approved` / `.explain()` let callers gate and log the choice.
    """
    warnings: list[str] = []
    requested: Optional[Decimal] = None
    source = "default"

    # ── 1. explicit ──────────────────────────────────────────────────────────
    if explicit is not None:
        requested = _to_decimal(explicit)
        source = "explicit"
        if requested is None:
            warnings.append(f"explicit value {explicit!r} invalid — falling back")

    # ── 2. env var ───────────────────────────────────────────────────────────
    if requested is None and env_var:
        raw = os.environ.get(env_var)
        if raw:
            requested = _to_decimal(raw)
            source = "env"
            if requested is None:
                warnings.append(f"{env_var}={raw!r} invalid — falling back")

    # ── 3. interactive (attended only) ───────────────────────────────────────
    if requested is None and interactive and sys.stdin.isatty():
        suggested = default
        if usdc_balance is not None:
            suggested = _fmt(_default_from_balance(usdc_balance, pct_of_balance, default))
        while requested is None:
            try:
                entry = input(f"  {prompt} [${suggested}]: ").strip().lstrip("$")
            except (EOFError, KeyboardInterrupt):
                entry = ""
                break
            requested = _to_decimal(entry or suggested)
            if requested is None:
                print("  ! enter a positive number, e.g. 0.05")
        source = "interactive"

    # ── 4. policy rule (pct of balance) ──────────────────────────────────────
    if requested is None and pct_of_balance is not None and usdc_balance is not None:
        requested = Decimal(str(usdc_balance)) * Decimal(str(pct_of_balance))
        requested = _to_decimal(requested)
        source = "policy"

    # ── 5. default ───────────────────────────────────────────────────────────
    if requested is None:
        requested = _to_decimal(default) or Decimal("0.02")
        source = "default"

    chosen = requested

    # ── Clamp to [min_cap, max_cap] ──────────────────────────────────────────
    lo, hi = _to_decimal(min_cap), _to_decimal(max_cap)
    if hi is not None and chosen > hi:
        warnings.append(f"requested ${_fmt(chosen)} exceeds max_cap ${_fmt(hi)} — capped")
        chosen = hi
    if lo is not None and chosen < lo:
        warnings.append(f"requested ${_fmt(chosen)} below min_cap ${_fmt(lo)} — raised")
        chosen = lo

    # ── Clamp to spendable balance (balance - reserve) ───────────────────────
    capped_by_balance = False
    if usdc_balance is not None:
        spendable = Decimal(str(usdc_balance)) - (_to_decimal(reserve) or Decimal("0"))
        if spendable < 0:
            spendable = Decimal("0")
        if chosen > spendable:
            warnings.append(
                f"cap ${_fmt(chosen)} exceeds spendable balance ${_fmt(spendable)} "
                f"— the balance is the real ceiling"
            )
            chosen = spendable
            capped_by_balance = True

    # ── Approval gate ────────────────────────────────────────────────────────
    needs_approval = False
    threshold = _to_decimal(approve_above)
    if threshold is not None and chosen > threshold:
        if approver is not None:
            needs_approval = not bool(approver(chosen))
        else:
            needs_approval = True
        if needs_approval:
            warnings.append(
                f"cap ${_fmt(chosen)} exceeds approval threshold ${_fmt(threshold)} "
                f"— requires explicit approval"
            )

    return BudgetDecision(
        max_spend=_fmt(chosen),
        source=source,
        requested=_fmt(requested) if requested is not None else None,
        capped_by_balance=capped_by_balance,
        needs_approval=needs_approval,
        warnings=warnings,
    )


def _default_from_balance(balance: float, pct: Optional[float], default: str) -> Decimal:
    if pct is not None:
        d = _to_decimal(Decimal(str(balance)) * Decimal(str(pct)))
        if d is not None:
            return d
    return _to_decimal(default) or Decimal("0.02")


def _fmt(amount) -> str:
    """Format a Decimal as a clean string, trailing zeros stripped."""
    s = f"{Decimal(str(amount)):.6f}".rstrip("0").rstrip(".")
    return s or "0"
