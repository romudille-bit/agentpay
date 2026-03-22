#!/usr/bin/env python3
"""
mcp_server.py — AgentPay MCP Server

Exposes all AgentPay tools as MCP tools with automatic x402 payment handling.
When Claude (or any MCP client) calls a tool, this server:
  1. POSTs to /tools/{name}/call → receives 402 challenge
  2. Pays via Stellar USDC
  3. Retries with X-Payment header
  4. Returns the data result

Usage:
    python gateway/mcp_server.py

Required env vars:
    STELLAR_SECRET_KEY      — Stellar account that pays for tool calls
    AGENTPAY_GATEWAY_URL    — Gateway URL (default: production)
"""

import os
import sys
import json
import asyncio
import logging
from typing import Any

import httpx
from stellar_sdk import Keypair, Server as StellarServer, Network, Asset, TransactionBuilder
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_URL = os.environ.get(
    "AGENTPAY_GATEWAY_URL",
    "https://gateway-production-2cc2.up.railway.app",
).rstrip("/")

STELLAR_SECRET_KEY = os.environ.get("STELLAR_SECRET_KEY", "")

USDC_ISSUER_TESTNET = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
USDC_ISSUER_MAINNET = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"

# Silence all non-critical logs — MCP communicates over stdio and any stray
# output will corrupt the protocol stream.
logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ── Tool registry cache ───────────────────────────────────────────────────────

_TOOLS: list[dict] = []


async def _fetch_tools() -> list[dict]:
    """Pull live tool definitions from the AgentPay registry."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{GATEWAY_URL}/tools")
        resp.raise_for_status()
        return resp.json()["tools"]


# ── Stellar payment (sync, run in executor) ───────────────────────────────────

def _send_usdc_payment(
    secret_key: str,
    destination: str,
    amount: str,
    memo: str,
    network: str = "testnet",
) -> str:
    """Send USDC on Stellar. Returns the transaction hash."""
    kp = Keypair.from_secret(secret_key)

    if network == "testnet":
        horizon_url = "https://horizon-testnet.stellar.org"
        network_passphrase = Network.TESTNET_NETWORK_PASSPHRASE
        usdc_issuer = USDC_ISSUER_TESTNET
    else:
        horizon_url = "https://horizon.stellar.org"
        network_passphrase = Network.PUBLIC_NETWORK_PASSPHRASE
        usdc_issuer = USDC_ISSUER_MAINNET

    stellar = StellarServer(horizon_url)
    account = stellar.load_account(kp.public_key)
    usdc = Asset("USDC", usdc_issuer)

    tx = (
        TransactionBuilder(
            source_account=account,
            network_passphrase=network_passphrase,
            base_fee=100,
        )
        .add_text_memo(memo[:28])          # Stellar memo limit = 28 chars
        .append_payment_op(destination=destination, asset=usdc, amount=amount)
        .set_timeout(30)
        .build()
    )
    tx.sign(kp)
    result = stellar.submit_transaction(tx)
    return result["hash"]


# ── x402 payment flow ─────────────────────────────────────────────────────────

async def _call_with_payment(tool_name: str, params: dict) -> dict:
    """
    Full x402 flow:
      1. POST → 402 challenge
      2. Pay USDC on Stellar
      3. Retry → 200 result
    """
    if not STELLAR_SECRET_KEY:
        raise RuntimeError(
            "STELLAR_SECRET_KEY is not set. "
            "Get a testnet wallet: curl https://gateway-production-2cc2.up.railway.app/faucet"
        )

    kp = Keypair.from_secret(STELLAR_SECRET_KEY)
    agent_address = kp.public_key

    async with httpx.AsyncClient(timeout=45.0) as client:
        # ── Step 1: Initial POST → expect 402 ────────────────────────────────
        r1 = await client.post(
            f"{GATEWAY_URL}/tools/{tool_name}/call",
            json={"parameters": params, "agent_address": agent_address},
        )

        if r1.status_code == 200:
            # Already paid (shouldn't happen on first call, but handle it)
            return r1.json()

        if r1.status_code != 402:
            raise RuntimeError(f"Unexpected status {r1.status_code}: {r1.text[:300]}")

        challenge = r1.json()
        payment_id  = challenge["payment_id"]
        amount_usdc = challenge["amount_usdc"]
        pay_to      = challenge["pay_to"]
        network     = challenge.get("network", "testnet")

        # ── Step 2: Pay on Stellar (blocking, run in thread) ─────────────────
        loop = asyncio.get_event_loop()
        tx_hash = await loop.run_in_executor(
            None,
            lambda: _send_usdc_payment(
                STELLAR_SECRET_KEY, pay_to, amount_usdc, payment_id, network
            ),
        )

        # ── Step 3: Retry with payment proof ─────────────────────────────────
        r2 = await client.post(
            f"{GATEWAY_URL}/tools/{tool_name}/call",
            json={"parameters": params, "agent_address": agent_address},
            headers={
                "X-Payment": f"tx_hash={tx_hash},from={agent_address},id={payment_id}",
            },
        )
        r2.raise_for_status()
        return r2.json()


# ── MCP Server ────────────────────────────────────────────────────────────────

server = Server("agentpay")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    global _TOOLS
    if not _TOOLS:
        _TOOLS = await _fetch_tools()

    mcp_tools = []
    for t in _TOOLS:
        desc = t["description"]
        if t.get("use_when"):
            desc += f"\n\nUse when: {t['use_when']}"
        if t.get("returns"):
            desc += f"\nReturns: {t['returns']}"
        if t.get("response_example"):
            import json as _json
            desc += f"\nExample response: {_json.dumps(t['response_example'])}"
        desc += f"\n\nPrice: ${t['price_usdc']} USDC per call"

        input_schema = t.get("parameters") or {"type": "object", "properties": {}}

        mcp_tools.append(types.Tool(
            name=t["name"],
            description=desc,
            inputSchema=input_schema,
        ))

    return mcp_tools


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        response = await _call_with_payment(name, arguments)
        tool_result = response.get("result", response)
        payment     = response.get("payment", {})

        # Format result as pretty JSON
        text = json.dumps(tool_result, indent=2)

        # Append payment receipt
        if payment.get("amount_usdc"):
            tx = payment.get("tx_hash", "")
            text += f"\n\n[Paid ${payment['amount_usdc']} USDC | tx: {tx[:20]}...]"

        return [types.TextContent(type="text", text=text)]

    except Exception as e:
        return [types.TextContent(
            type="text",
            text=f"AgentPay error calling '{name}': {e}",
        )]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    # Pre-fetch tools so list_tools() responds instantly
    global _TOOLS
    try:
        _TOOLS = await _fetch_tools()
        print(f"AgentPay MCP: loaded {len(_TOOLS)} tools from {GATEWAY_URL}", file=sys.stderr)
    except Exception as e:
        print(f"AgentPay MCP: could not pre-fetch tools ({e}), will retry on first request", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
