"""Sponsored Stacks payments: the payer signs a sponsored tx (SIP-005 auth
0x05, fee 0, placeholder sponsor condition) and a relay co-signs, pays the STX
fee and broadcasts. VelumX is the relay (POST {VELUMX_API_URL}/broadcast).

The relayed txid differs from the hash of the payer-signed bytes (the sponsor
condition is hashed too), so the replay store keys on the payer's bytes and
payment_logs.tx_hash carries the relayed txid. An ambiguous relay answer is
resolved by finding the tx again by (sender, nonce) on Hiro.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from gateway.config import settings

logger = logging.getLogger("agentpay.gateway.stacks")

RELAY = "velumx"


def sponsor_enabled() -> bool:
    return settings.STACKS_SPONSOR == RELAY and bool(settings.VELUMX_API_KEY)


def sponsorship_fields() -> dict:
    """`payment_options.stacks.sponsorship`; absent when no relay is configured."""
    if not sponsor_enabled():
        return {}
    return {"sponsorship": {
        "available": True,
        "relay": RELAY,
        "origin_fee_microstx": 0,
        "note": "sign the sponsored auth variant with fee 0; the relay pays the STX fee",
    }}


def _normalize_txid(txid) -> str:
    return str(txid or "").strip().lower().removeprefix("0x")


async def relay_sponsored(signed_tx: bytes, network: str) -> dict:
    """Hand the payer-signed sponsored tx to the relay.

    Returns {"outcome": "accepted" | "rejected" | "uncertain", "txid", "reason"}.
    A 4xx or a connection that never opened means nothing was broadcast; a
    failure after the request left may have.
    """
    url = f"{settings.VELUMX_API_URL.rstrip('/')}/broadcast"
    headers = {"x-api-key": settings.VELUMX_API_KEY, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=settings.STACKS_SETTLE_TIMEOUT_S) as client:
            resp = await client.post(url, json={"txHex": signed_tx.hex(), "network": network},
                                     headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        return {"outcome": "rejected", "txid": "",
                "reason": f"sponsor_unreachable: {str(e)[:100]}"}
    except Exception as e:
        return {"outcome": "uncertain", "txid": "", "reason": f"sponsor_error: {str(e)[:100]}"}

    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code < 300:
        txid = _normalize_txid(data.get("txid") or data.get("txId"))
        if not txid:
            return {"outcome": "uncertain", "txid": "", "reason": "sponsor_no_txid"}
        return {"outcome": "accepted", "txid": txid, "reason": "ok"}
    err = str(data.get("error") or data.get("message") or resp.text[:160] or resp.status_code)
    if resp.status_code < 500:
        return {"outcome": "rejected", "txid": "", "reason": f"sponsor_rejected: {err[:160]}"}
    return {"outcome": "uncertain", "txid": "",
            "reason": f"sponsor_http_{resp.status_code}: {err[:120]}"}


async def find_relayed_tx(hiro_api: str, sender: str, nonce: int) -> Optional[str]:
    """The txid holding (sender, nonce) in Hiro's mempool or confirmed list, or None."""
    if not sender or nonce < 0:
        return None
    paths = (f"/extended/v1/address/{sender}/mempool?limit=50",
             f"/extended/v1/address/{sender}/transactions?limit=50")
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for path in paths:
            try:
                resp = await client.get(f"{hiro_api}{path}")
                rows = resp.json().get("results", []) if resp.status_code == 200 else []
            except Exception as e:
                logger.warning(f"[STACKS] relayed-tx lookup failed for {sender[:8]}…: {e}")
                continue
            for row in rows:
                tx = row.get("tx", row)
                if int(tx.get("nonce", -1)) == nonce and tx.get("sender_address") == sender:
                    return _normalize_txid(tx.get("tx_id"))
    return None
