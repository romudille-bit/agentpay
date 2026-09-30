"""
routes/mcp_proxy.py — https://agentpay.tools/mcp → the remote MCP service (AGE-127).

The Streamable-HTTP MCP server is the Node package running as its own Railway
service (npm/railway.toml). This route keeps the public URL on the gateway's
domain: the body and the MCP headers go through untouched, the reply streams
back (SSE or JSON), and nothing here parses JSON-RPC. Unset MCP_UPSTREAM_URL
means "not deployed" and answers 503 with the local alternative.
"""

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Request headers the MCP transport cares about; everything else (cookies,
# Cloudflare/Railway hop headers) stays on this side.
_FORWARD_REQ = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version",
                "last-event-id", "user-agent")
# Response headers to relay. Hop-by-hop and length headers are dropped: the
# stream is re-chunked here.
_FORWARD_RESP = ("content-type", "mcp-session-id", "mcp-protocol-version",
                 "cache-control", "allow")

# A tool call can take ~45 s (the Node server's own upstream timeout); the
# stream itself is read chunk by chunk, so the read budget is per chunk.
_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)


def _upstream() -> str:
    return (settings.MCP_UPSTREAM_URL or "").rstrip("/")


@router.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def mcp_proxy(request: Request):
    upstream = _upstream()
    if not upstream:
        return JSONResponse(status_code=503, content={
            "error": "remote MCP not deployed on this gateway",
            "local": "npx -y @romudille/agentpay-mcp",
        })

    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQ}
    fwd = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")
    if fwd:
        headers["x-forwarded-for"] = fwd
    body = await request.body()

    client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        req = client.build_request(request.method, f"{upstream}/mcp", headers=headers, content=body)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("mcp proxy: upstream unreachable: %s", exc)
        return JSONResponse(status_code=502, content={"error": "remote MCP unavailable"})

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() in _FORWARD_RESP}
    return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)
