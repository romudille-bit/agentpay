"""
routes/infra.py — Basic gateway-status endpoints.

  GET / | HEAD /     — landing page (HTML) for browsers, JSON manifest for agents
  GET /health        — Railway healthcheck target
  GET /stats         — pending payments + recent transaction tail
"""

import base64

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import registry

from gateway.config import GATEWAY_URL, settings
from gateway.landing import render_landing
from gateway.services.transaction_log import recent_transactions
from gateway.x402 import get_pending_count

router = APIRouter()


@router.api_route("/", methods=["GET", "HEAD"])
async def root(request: Request):
    """Content-negotiated root.

    Browsers (Accept: text/html) get the landing page. Agents and API clients
    get the JSON manifest. HEAD requests run GET and drop the body, so both
    content types respond with 200 — keeps the Bazaar "quality score" check
    happy regardless of which Accept the indexer sends.
    """
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return HTMLResponse(content=render_landing(registry.list_tools(), GATEWAY_URL))

    return JSONResponse(content={
        "name":             "AgentPay",
        "tagline":          "Economic intelligence for autonomous agents",
        "version":          "1.0",
        "tools":            len(registry.list_tools()),
        "docs":             "https://github.com/romudille-bit/agentpay",
        "tools_endpoint":   f"{GATEWAY_URL}/tools",
        "faucet":           f"{GATEWAY_URL}/faucet",
        "discovery":        f"{GATEWAY_URL}/.well-known/agentpay.json",
        "payment_networks": ["base", "stellar"],
    })


_FAVICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <!-- dark rounded background -->
  <rect width="32" height="32" rx="7" fill="#0a0a0b"/>
  <!-- teal "A" mark — two legs + crossbar -->
  <path d="M16 5 L26 27 H22 L19.5 21 H12.5 L10 27 H6 Z M14 17 H18 L16 12 Z"
        fill="#5eead4"/>
</svg>
"""


@router.get("/favicon.svg", response_class=Response)
async def favicon():
    """SVG favicon — dark background, teal A mark."""
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# Multi-size ICO (16/32/48) of the same dark-bg + teal-A mark. Some directories
# (e.g. x402scan) probe /favicon.ico specifically to render a listing icon, so we
# serve a real .ico in addition to the SVG above.
_FAVICON_ICO = base64.b64decode(
    "AAABAAIAEBAAAAAAIAB8AgAAJgAAACAgAAAAACAASAIAAKICAACJUE5HDQoaCgAAAA1JSERSAAAAEAAA"
    "ABAIBgAAAB/z/2EAAAJDSURBVHicbZPPS1RRFMc/5743zZs3KQ7YpOaYiRizCLICrQzDlAlKclOtg7ZB"
    "m1bRyr+hrdvatEmCxBa6kIKiHwvFEoSyVpUpib335r17Wjg/s7u7957zOfd87/cIgO/7JZAHwDAggOH/"
    "ywIKvAKd3t3dnRPPOzhqjD4HvMqlAIgxiNnjqE1Qq1VINSawVi5LJpN9a4wMqmoMuAAiQvwnILYxACk3h"
    "ZNOo1qDxCLiWqvvxPez+m/lOAw4dGaQrtIomiRszM7za3kVN+2h1ja9xK301dyzVYp3b9N/4zqCkMm38/"
    "LOffCkMUoA6zYli5CEIa19veTPnmJrfRXH8+gYO4/fkSfa3EZSLtRbMU2VxTEk5YjO8RFaC31sPJ3n29wC"
    "ueNFDl8YJo6CmrA1QuNG4wTXy9A9OUESh3x+8oyvsy8wjkPh6jjGcUDt/wFiDEkQkDtRJD98mq1PH/m9/oW"
    "tlTW2N9bpHDtHy7GjJEEEUtfCbezf2oTuK5dw/AzZwhGmVhZqcJNy6SpdZOXhDGnfQ+OkASCClsuk29ooTE6"
    "gSczazGOC7z9Ra2np7aH/1k16rpVYm3lUS64CrIiYchjSNTJEtruTH68/8ObeNFE5QoFsLkf70ElaB/poKw6w"
    "+X4ZJ+OBqq0bSVUcz8OkUyRBhMYx4uxJZOMEJ30Ak0qRhBE2DEFEAWmyslrrqrVNc1D7IWvBWjAOYqRu5X3DJ"
    "BWJ62apiVw5bxomEwQ7i6BTwBJgUbX7kqtAVcue9ZdAp4JgZ/Evi2D9OVNcjP4AAAAASUVORK5CYIKJUE5HDQ"
    "oaCgAAAA1JSERSAAAAIAAAACAIBgAAAHN6evQAAAIPSURBVHicY2TAAri4uP9jE6cUfPv2lRFdDEWAVhbjcwgT"
    "vS1Ht4uJ3pajO4KJkEJaA8aB8D0yGPAQYCFHk8f+NQw8irIoYk+27mU4kV1Fslkkh4CwkS6G5QwMDAySLrYMr"
    "Lw8tHeAXJAnVnFmdjYGGR8X2jqAiZWVQdbHFae8fCB2x1HNAZLONgxsAnxw/rsLVxl+ffwM54uY6jNwy0rRzgHy"
    "aMH/aONOhme7DyIEGBkZ5EgMBaIdwCbAxyDhaIUQ+P+f4en2fQxPtu5FdSStHCDr58bAxMoK5789e4nh+4vXDK+"
    "OnGb4/QkRDTyKsgxChjrUd4B8kBcK/8k2iM///f7N8Gz3ITS1xIcCUQ7gVZRjEDLQRgj8/8/wZPt+hGPQokHWx"
    "xUltCh2gFwwqu/fnr/C8P35Kzj/5eFTDL8/f4Hz2QT5UdMLHkC4MmJkZPA8vJ6BW0aSKANh4OmO/QzHMyoIqiMY"
    "AqJmhiRbzsCAWWaQ7QB5tOAnFjCxsjLIeBMumvHWhszsbAzSXk4oYifzahkeb9qFVb1eVR6DWlo0nC8f7MVwb+k"
    "6/A7FJynlZs/AysMN5//5+h0jyyGDRxt2oPCFjXQZeBRkyHcAet5/umM/w9/vP3Cq/3DtFsOn2/dRxOQC8UfhaJN"
    "s4B2ArbtEL/Dt21fGgQ8BmEvobTHMTiZ0AXpazsCA1juGAXp2zwEDZZYOno3qYQAAAABJRU5ErkJggg=="
)


@router.get("/favicon.ico", response_class=Response)
async def favicon_ico():
    """ICO favicon — same dark-bg + teal-A mark, for directories that probe .ico."""
    return Response(content=_FAVICON_ICO, media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "network": settings.STELLAR_NETWORK,
        "gateway_address": settings.GATEWAY_PUBLIC_KEY or "NOT_CONFIGURED",
        "pending_payments": get_pending_count(),
    }


@router.get("/stats")
async def stats():
    """Gateway statistics."""
    tools = registry.list_tools()
    total_calls = sum(t.total_calls for t in tools)
    return {
        "total_tools": len(tools),
        "total_calls": total_calls,
        "recent_transactions": recent_transactions(10),
        "pending_payments": get_pending_count(),
        "network": settings.STELLAR_NETWORK,
    }
