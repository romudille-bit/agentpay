"""Inputs a paid call must state before the gateway settles it.

A paid call that leaves these out used to settle and run on silent defaults.
The buyer now gets a 400 that names what is missing and suggests the
defaults, before anything is charged.
"""
from fastapi.responses import JSONResponse

# tool -> required inputs, accepted aliases, and the defaults to suggest.
_RULES: dict[str, dict] = {
    "pre_trade_check": {
        "required": ("symbol",),
        "aliases": {"symbol": ("asset",)},
        "suggested": {"symbol": "ETH", "side": "long", "size_usd": 10000},
    },
    "verified_route": {
        "required": ("need",),
        "aliases": {},
        # No sensible default for a need; this one is an example only.
        "suggested": {"need": "crypto prices"},
    },
    "session_create": {
        "required": ("max_spend",),
        "aliases": {},
        "suggested": {"max_spend": "0.10"},
    },
}


def _present(params: dict, key: str, aliases: tuple) -> bool:
    return any(str(params.get(k) or "").strip() for k in (key, *aliases))


def missing(tool: str, params: dict | None) -> list[str]:
    """Required inputs the caller did not send; empty for tools with no rule."""
    rule = _RULES.get(tool)
    if not rule:
        return []
    params = params if isinstance(params, dict) else {}
    return [k for k in rule["required"]
            if not _present(params, k, rule["aliases"].get(k, ()))]


def suggested(tool: str, params: dict | None) -> dict:
    """The defaults with the caller's own values on top: resendable as-is."""
    rule = _RULES.get(tool) or {}
    params = params if isinstance(params, dict) else {}
    sent = {k: v for k, v in params.items() if str(v if v is not None else "").strip()}
    return {**rule.get("suggested", {}), **sent}


def hint(tool: str, params: dict | None) -> dict:
    """Extra keys for the 402 body, so a client can fix its inputs before paying."""
    rule = _RULES.get(tool)
    if not rule:
        return {}
    return {"required_parameters": list(rule["required"]),
            "suggested_parameters": suggested(tool, params)}


def refusal(tool: str, params: dict | None) -> JSONResponse | None:
    """400 for a paid call with required inputs missing; None when complete.
    Call it before any settle step: nothing has been charged at that point."""
    gaps = missing(tool, params)
    if not gaps:
        return None
    return JSONResponse(status_code=400, content={
        "error":   "missing_required_parameters",
        "tool":    tool,
        "missing": gaps,
        "suggested_parameters": suggested(tool, params),
        "charged": False,
        "message": ("Nothing was charged and your payment was not used. Resend "
                    "the same payment with suggested_parameters to accept the "
                    "defaults, or with your own values."),
    })
