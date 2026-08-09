"""AGE-113: keep our own Bazaar listing alive — check free, pay only if needed.

Bazaar re-indexes a resource from its LIVE 402 at settle time. A listing with
no recurring paid traffic goes stale and drops out of the index entirely:
measured 2026-08-09, `session_create` fell out within three days of its forced
re-index (AGE-111), taking `budget cap`, `spend control` and `session` with it
— the spend-governance category we deliberately chose to own. `verified_route`
and `pre_trade_check` survived only because a real customer buys them every
1-3h, which refreshes their records for free.

So the fix is not a bigger name or a one-off reindex. It is recurring traffic.

Design — check free, settle only on a miss:

  * The check is a free, read-only Bazaar search. Cadence guessing is avoided
    entirely: we do not settle on a schedule and hope it is inside the decay
    window, we settle when the listing is ACTUALLY missing.
  * A settle costs $0.01 and only happens on a confirmed miss, so the steady
    state is $0.00 for as long as the listing holds.
  * It FAILS CLOSED on spending: if the Bazaar query errors, times out,
    returns something we can't parse, OR returns an empty/missing resources
    list, we do NOT pay. An unreachable index is not evidence that we are
    missing from it — and the brand query can never legitimately come back
    empty while eight rival "AgentPay" products exist, so an empty result is
    an API change or a degraded index, not absence.
  * How often this has to fire IS the decay-rate measurement AGE-113 asks for.
    Every fire is logged, so the run log is the dataset.

On honesty: the flagship analyst is a disclosed AgentPay-operated agent that
pays for AgentPay tools with its own funded wallet (see this package's run.py
docstring — it is a real customer by design, and tools/reindex_bazaar.py
already names this wallet "disclosed paying customer"). A keepalive settle is
a real payment for a real product, not invented revenue. It must never be
counted as third-party demand: the payer address is ours and already excluded
wherever customer counts are derived. Keep it that way.

All I/O is injected, so the tests need no network and no wallet.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

BAZAAR_SEARCH = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
UA = "agentpay-keepalive/1.0 (+https://agentpay.tools)"

# The brand term deliberately: it returns a SMALL result set (2 items on
# 2026-08-09), so absence from it is real absence rather than a ranking slip.
# A head term could miss us for ranking reasons and trigger a pointless spend.
PROBE_QUERY = "agentpay"

# The canonical resource AGE-112 made both payable paths declare.
SESSION_RESOURCE_URL = "https://agentpay.tools/v1/session/create"

KEEPALIVE_PARAMS = {"max_spend": "0.10"}


def _search(query: str, timeout: int = 20) -> dict:
    url = f"{BAZAAR_SEARCH}?query={urllib.parse.quote(query)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def indexed_urls(payload: dict) -> set[str]:
    """Every resource url in a Bazaar search payload, however it is shaped.

    Bazaar returns `resource` as either a bare url string or an object with a
    `url` field; tolerate both rather than trusting one shape.
    """
    out: set[str] = set()
    for row in (payload or {}).get("resources", []) or []:
        res = row.get("resource")
        url = res if isinstance(res, str) else (res or {}).get("url", "")
        if url:
            out.add(url.rstrip("/"))
    return out


def is_indexed(payload: dict, resource_url: str = SESSION_RESOURCE_URL) -> bool:
    return resource_url.rstrip("/") in indexed_urls(payload)


def keepalive(session, log, *, search=_search, query: str = PROBE_QUERY,
              resource_url: str = SESSION_RESOURCE_URL,
              enabled: bool = True, price=None) -> dict:
    """Check the listing; settle one $0.01 session_create only if it is gone.

    `session` is the analyst's live Session (budget cap enforced by it).
    Returns a JSON-safe dict for the run payload. Never raises.
    """
    if not enabled:
        return {"ran": False, "reason": "disabled"}

    try:
        payload = search(query)
    except Exception as e:
        # Fail closed: an unreachable index is not proof we are absent.
        log(f"keepalive: Bazaar check failed ({type(e).__name__}: {e}) "
            f"— not settling")
        return {"ran": True, "indexed": None, "settled": False,
                "reason": f"check failed: {type(e).__name__}"}

    rows = payload.get("resources") if isinstance(payload, dict) else None
    if not rows:
        # Also fail closed. The brand query can never legitimately be empty —
        # eight rival "AgentPay" products guarantee matches — so an empty or
        # missing resources list means the API changed shape or the index is
        # degraded, not that we are absent. Treating it as absence would buy
        # a $0.01 settle on every run until someone noticed the log line.
        log(f"keepalive: implausible Bazaar payload for {query!r} "
            f"(resources missing/empty) — not settling")
        return {"ran": True, "indexed": None, "settled": False,
                "reason": "implausible payload: resources missing/empty"}

    if is_indexed(payload, resource_url):
        log("keepalive: session listing is indexed — nothing to do ($0.00)")
        return {"ran": True, "indexed": True, "settled": False,
                "reason": "already indexed"}

    log(f"keepalive: session listing MISSING from Bazaar "
        f"({len(indexed_urls(payload))} results for {query!r}) — refreshing it")

    cost = price if price is not None else (
        session.tool_cost_usd("session_create") or _default_cost())
    if session.would_exceed(cost):
        log("keepalive: budget cap reached — listing left stale")
        return {"ran": True, "indexed": False, "settled": False,
                "reason": "budget cap reached"}

    try:
        r = session.call("session_create", dict(KEEPALIVE_PARAMS))
    except Exception as e:
        # Same lesson as 2026-08-07: a paid-call failure degrades, never kills.
        log(f"keepalive: settle failed ({type(e).__name__}: {e})")
        return {"ran": True, "indexed": False, "settled": False,
                "reason": f"settle failed: {type(e).__name__}"}

    tx = getattr(r, "tx", None)
    log(f"keepalive: settled session_create to refresh the listing | tx {tx}")
    return {"ran": True, "indexed": False, "settled": True, "tx": tx,
            "reason": "listing was de-indexed"}


def _default_cost():
    from decimal import Decimal
    return Decimal("0.01")
