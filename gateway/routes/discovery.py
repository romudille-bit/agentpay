"""
routes/discovery.py — Discovery + manifest endpoints.

  GET /.well-known/agentpay.json       — AgentPay manifest
  GET /.well-known/agent.json          — A2A protocol agent card
  GET /.well-known/l402-services       — 402index.io discovery format
  GET /.well-known/x402                — x402 protocol manifest
  GET /.well-known/402index-verify.txt — 402index.io domain proof
  GET /robots.txt                      — search-engine policy
  GET /llms.txt                        — LLM-readable service description
  GET /sitemap.xml                     — sitemap covering all public URLs
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import registry

from gateway.config import GATEWAY_URL, settings

router = APIRouter()


@router.get("/.well-known/agentpay.json")
async def well_known_agentpay():
    """AgentPay manifest — discoverable by x402-aware agents."""
    tools = registry.list_tools()
    return {
        "name": "AgentPay",
        "version": "1.0",
        "tagline": "Your agent is only as smart as its data",
        "description": "Real-time crypto data for AI agents. Pay per call in USDC on Stellar. No API keys, no subscriptions.",
        "url": GATEWAY_URL,
        "payment_protocol": "x402",
        "payment_network": f"stellar-{settings.STELLAR_NETWORK}",
        "payment_asset": "USDC",
        "pricing_model": "per-call",
        "budget_aware": True,
        "faucet": f"{GATEWAY_URL}/faucet",
        "tools_endpoint": f"{GATEWAY_URL}/tools",
        "capabilities": ["market-data", "onchain-analytics", "defi", "sentiment", "whale-tracking"],
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "price_usdc": t.price_usdc,
                "category": t.category,
                "parameters": t.parameters,
                "endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "triggers": t.triggers,
                "use_when": t.use_when,
                "returns": t.returns,
                "response_example": t.response_example,
            }
            for t in tools
        ],
    }


@router.get("/.well-known/agent.json")
async def well_known_agent():
    """A2A protocol card — agent-to-agent discovery."""
    tools = registry.list_tools()
    return {
        "name": "AgentPay Data Gateway",
        "description": "Autonomous crypto data tools for AI agents",
        "url": GATEWAY_URL,
        "version": "1.0",
        "capabilities": {
            "tools": True,
            "payments": "x402/stellar",
            "budget_sessions": True,
        },
        "contact": "https://github.com/romudille-bit/agentpay",
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "price_usdc": t.price_usdc,
                "category": t.category,
                "call_endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "triggers": t.triggers,
                "use_when": t.use_when,
                "returns": t.returns,
            }
            for t in tools
        ],
    }


@router.get("/.well-known/l402-services")
async def well_known_l402_services():
    """402index.io discovery document — lists all paid endpoints with pricing and request schemas."""
    tools = registry.list_tools()

    def _request_body(tool) -> dict:
        """Convert JSON-Schema parameters to 402index request_body format."""
        props = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        return {
            field: {
                **spec,
                "required": field in required,
            }
            for field, spec in props.items()
        }

    return {
        "version": "0.2.0",
        "name": "AgentPay",
        "description": "Real-time crypto data for AI agents. Pay per call in USDC on Stellar or Base. No API keys.",
        "homepage": GATEWAY_URL,
        "protocol": "x402",
        "protocols": ["x402"],
        "payment_network": "stellar",
        "services": [
            {
                "id": t.name,
                "name": t.name.replace("_", " ").title(),
                "description": t.description,
                "endpoint": f"{GATEWAY_URL}/tools/{t.name}/call",
                "method": "POST",
                "content_type": "application/json",
                "pricing": {
                    "amount": float(t.price_usdc),
                    "currency": "USD",
                    "type": "fixed",
                },
                "request_body": _request_body(t),
            }
            for t in tools
        ],
    }


@router.get("/.well-known/x402")
async def well_known_x402():
    """
    x402 protocol discovery manifest.
    Scanners and agents probing /.well-known/x402 find supported networks,
    assets, pricing range, and facilitator info here.
    """
    tools = registry.list_tools()
    prices = [float(t.price_usdc) for t in tools]
    return {
        "x402Version": 1,
        "gateway": GATEWAY_URL,
        "name": "AgentPay",
        "description": "Real-time crypto data for AI agents. Pay per call in USDC on Stellar or Base.",
        "accepts": [
            {
                "scheme": "exact",
                "network": "stellar-mainnet",
                "asset": "USDC",
                "assetIssuer": settings.USDC_ISSUER_MAINNET,
                "minAmount": str(min(prices)),
                "maxAmount": str(max(prices)),
                "facilitator": settings.STELLAR_FACILITATOR_URL,
            },
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base mainnet
                "minAmount": str(min(prices)),
                "maxAmount": str(max(prices)),
            },
        ],
        "endpoints": [
            {
                "path": f"/tools/{t.name}/call",
                "method": "POST",
                "amountRequired": t.price_usdc,
            }
            for t in tools
        ],
    }


@router.get("/.well-known/402index-verify.txt", response_class=Response)
async def well_known_402index_verify():
    """
    Domain-verification file for 402index.io.

    Serves the SHA-256 hash from INDEX402_VERIFY_HASH as plain text. Returns
    404 when the env var is empty so the endpoint is harmless until claimed.
    """
    if not settings.INDEX402_VERIFY_HASH:
        raise HTTPException(status_code=404, detail="Not configured")
    return Response(
        content=settings.INDEX402_VERIFY_HASH + "\n",
        media_type="text/plain",
    )


@router.get("/robots.txt", response_class=Response)
async def robots():
    return Response(
        content=(
            "User-agent: *\n"
            "Allow: /\n"
            f"Sitemap: {GATEWAY_URL}/sitemap.xml\n"
        ),
        media_type="text/plain",
    )


@router.get("/llms.txt", response_class=Response)
async def llms_txt():
    tools = registry.list_tools()
    tool_lines = "\n".join(
        f"- {t.name} (${t.price_usdc}): {t.description}"
        for t in sorted(tools, key=lambda t: t.price_usdc)
    )
    content = f"""\
# AgentPay

> x402 payment gateway for AI agents. Agents pay micro-amounts of USDC on Stellar or Base to call real crypto data tools — no API keys, no subscriptions, pay-per-call.

AgentPay implements the x402 protocol: agents receive an HTTP 402 challenge, pay on-chain in USDC, then retry with an X-Payment header to receive data. All payments are verified on-chain. The gateway takes 15% and auto-splits 85% to the tool developer's Stellar wallet.

## Gateway

- Production (mainnet): {GATEWAY_URL}
- Network: Stellar mainnet + Base mainnet
- Tools: {len(tools)} live crypto data tools
- Protocol: x402 (HTTP 402 → pay → retry)

## Tools

{tool_lines}

## Integration

POST /tools/{{name}}/call with {{parameters, agent_address}}
On 402: pay USDC to the given Stellar or Base address, retry with X-Payment header.
Response: data is in result["result"]

## Docs

- README: {GATEWAY_URL}/
- MCP server: npx @romudille/agentpay-mcp
- npm: https://www.npmjs.com/package/@romudille/agentpay-mcp
- GitHub: https://github.com/romudille-bit/agentpay
- Glama MCP: https://glama.ai/mcp/servers/romudille-bit/agentpay
"""
    return Response(content=content, media_type="text/plain")


@router.get("/sitemap.xml", response_class=Response)
async def sitemap():
    tools = registry.list_tools()
    urls = [
        f"{GATEWAY_URL}/",
        f"{GATEWAY_URL}/tools",
        f"{GATEWAY_URL}/.well-known/agentpay.json",
        f"{GATEWAY_URL}/.well-known/agent.json",
        f"{GATEWAY_URL}/.well-known/x402",
        f"{GATEWAY_URL}/faucet/ui",
    ] + [f"{GATEWAY_URL}/tools/{t.name}" for t in tools]

    loc_tags = "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{loc_tags}
</urlset>"""
    return Response(content=xml, media_type="application/xml")
