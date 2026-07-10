"""
routes/plan.py — Pre-flight plan cost estimation.

  POST /v1/plan/estimate — price a multi-tool plan BEFORE spending anything.

The buyer-side differentiator: an agent submits the tool calls it intends to
make and gets back per-step cost, the total, a fits-budget verdict, and a
cheaper alternative per paid step — all free, no payment, no wallet needed.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

import registry
from gateway._limiter import limiter

logger = logging.getLogger(__name__)
router = APIRouter()

# Same alias map as routes/tools.py — plans should price what a call would hit.
_TOOL_ALIASES = {
    "dex_liquidity": "token_market_data",
}


class PlanStep(BaseModel):
    tool: str
    params: dict = Field(default_factory=dict)


class PlanEstimateRequest(BaseModel):
    steps: list[PlanStep]
    budget: Optional[str] = None   # USDC, e.g. "0.10"; omit for cost-only


def _cheapest_alternative(tool) -> Optional[dict]:
    """Cheapest active same-category tool strictly cheaper than `tool`."""
    candidates = [
        t for t in registry.list_tools(category=tool.category)
        if t.name != tool.name and t.active
        and Decimal(t.price_usdc) < Decimal(tool.price_usdc)
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda t: Decimal(t.price_usdc))
    return {"tool": best.name, "price_usdc": best.price_usdc}


def _external_step(url: str, score_row: Optional[dict]) -> tuple[dict, Optional[Decimal]]:
    """Annotate an external x402 URL step from the Prober's service_scores
    (AGE-8 / [MR-3]). Returns (entry, price) — price only when the Prober has
    seen the live 402 (last-known advertised amount), letting a plan cover
    external legs the registry can't price."""
    entry: dict = {"tool": url, "exists": True, "external": True}
    if not score_row:
        entry["probed"] = False
        return entry, None
    entry["probed"] = True
    from gateway.radar import _delivery_why
    why = _delivery_why(score_row)
    if why:
        entry["why"] = why
    if score_row.get("mpp_option"):
        entry["mpp_option"] = True     # label only — never settled by us
    if score_row.get("usdg_option"):
        entry["usdg_option"] = True    # label only — never settled by us
    if score_row.get("delivery_factor") is not None:
        entry["delivery_factor"] = score_row["delivery_factor"]
    price = None
    raw = score_row.get("price_usdc")
    if raw is not None:
        try:
            price = Decimal(str(raw))
            entry["price_usdc"] = format(price.normalize(), "f")
            entry["free"] = price == 0
            entry["price_source"] = "prober_last_seen_402"
        except (InvalidOperation, ValueError):
            price = None
    return entry, price


@router.post("/v1/plan/estimate")
@limiter.limit("60/minute")
async def estimate_plan(body: PlanEstimateRequest, request: Request):
    """Price a plan of tool calls without executing or paying for anything.

    Unknown tools don't fail the request — they come back with
    exists=false so the agent can re-plan around them. External x402 URLs
    are annotated (and priced when possible) from the Prober's telemetry.
    """
    steps_out = []
    total = Decimal("0")
    unknown = 0

    # Prober scores — fetched once, only when the plan has external URL steps.
    scores: dict = {}
    if any(s.tool.startswith(("http://", "https://")) for s in body.steps):
        from gateway.services.supabase import fetch_service_scores
        scores = await fetch_service_scores()

    for step in body.steps:
        if step.tool.startswith(("http://", "https://")):
            entry, price = _external_step(step.tool, scores.get(step.tool))
            if price is not None:
                total += price
            steps_out.append(entry)
            continue
        resolved = _TOOL_ALIASES.get(step.tool, step.tool)
        tool = registry.get_tool(resolved)
        if tool is None or not tool.active:
            unknown += 1
            steps_out.append({
                "tool":   step.tool,
                "exists": False,
                "error":  f"Tool '{step.tool}' not found or inactive",
            })
            continue
        price = Decimal(tool.price_usdc)
        total += price
        entry = {
            "tool":       resolved,
            "exists":     True,
            "price_usdc": tool.price_usdc,
            "free":       price == 0,
            "category":   tool.category,
        }
        if price > 0:
            alt = _cheapest_alternative(tool)
            if alt:
                entry["cheaper_alternative"] = alt
        steps_out.append(entry)

    def _fmt(d: Decimal) -> str:
        # Canonical decimal string: "0" not "0.000", no scientific notation.
        return format(d.normalize(), "f")

    result = {
        "steps":         steps_out,
        "total_usdc":    _fmt(total),
        "paid_calls":    sum(1 for s in steps_out if s.get("exists") and not s.get("free")),
        "free_calls":    sum(1 for s in steps_out if s.get("free")),
        "unknown_tools": unknown,
    }

    if body.budget is not None:
        try:
            budget = Decimal(str(body.budget))
            result["budget"] = _fmt(budget)
            result["fits_budget"] = total <= budget
            result["remaining_after"] = _fmt(budget - total) if total <= budget else None
        except (InvalidOperation, ValueError):
            result["budget_error"] = f"Unparseable budget: {body.budget!r}"

    return result
