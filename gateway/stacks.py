"""
gateway/stacks.py — Stacks/sBTC settlement adapter.

The gateway broadcasts: the client hands over a fully signed, unbroadcast sBTC
transfer and this module broadcasts it — via the facilitator's /settle, or
directly to Hiro if the facilitator is down (a convenience layer, not a hard
dependency). verify_stacks_payment statically checks the tx and recomputes its
txid from the bytes; settle_stacks_payment consumes that txid before broadcast
(fail-closed replay guard) and polls Hiro by txid on an ambiguous outcome.

Settle-response contract the SDK's retry logic keys on: "rejected" = the node
refused the tx (no mempool, can never settle) — never for an ambiguous timeout;
"uncertain" = broadcast may have happened, spend stays recorded. Full wire
contract: docs/stacks-adapter.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from decimal import ROUND_CEILING, Decimal
from typing import Optional

import httpx

from agentpay._stacks_tx import (
    SBTC_ASSET_NAME,
    SBTC_CONTRACT_MAINNET,
    SBTC_CONTRACT_TESTNET,
    STACKS_MAINNET_CAIP2,
    STACKS_TESTNET_CAIP2,
    c32_address,
    sats_from_usd,
    serialize_transfer_payload,
    txid_of,
    verify_origin_signature,
)
from gateway.config import settings
from gateway.services import supabase as sb

logger = logging.getLogger("gateway.stacks")

__all__ = [
    "verify_stacks_payment",
    "settle_stacks_payment",
    "poll_confirmation",
    "decode_sbtc_transfer",
    "decode_stacks_transfer",
    "decode_payment_signature",
    "build_stacks_402_option",
    "stacks_402_option",
    "stacks_offer",
    "stacks_offerable",
    "stacks_quote",
    "suggested_fee_microstx",
    "stacks_quote_sats",
    "stacks_configured",
    "stacks_accepts_entry",
    "stacks_accepts_entry_stx",
    "stacks_stx_quote",
    "stacks_stx_offerable",
    "payload_asset",
    "payload_network",
    "payload_signed_tx_hex",
    "payload_payment_id",
]

# In-memory fast guard for txid consumption (mirrors _used_base_tx_hashes in
# gateway/base.py — single-process guard when Supabase is disabled/unreachable).
# Insertion-ordered so it can be bounded: Supabase is the durable store, this
# only has to cover a restart-free window.
_used_stacks_txids: dict[str, None] = {}
_USED_TXIDS_MAX = 50_000


def _remember_txid(txid: str) -> None:
    _used_stacks_txids[txid] = None
    while len(_used_stacks_txids) > _USED_TXIDS_MAX:
        _used_stacks_txids.pop(next(iter(_used_stacks_txids)))

# Node rejection reasons that are definitive — the tx was refused at
# broadcast, is in no mempool, and can never settle. Only these may produce
# state "rejected" from a broadcast attempt. (Hiro /v2/transactions error
# body: {"error": "transaction rejected", "reason": "<one of these>", ...})
_DEFINITIVE_REJECTIONS = (
    "BadNonce",
    "ConflictingNonceInMempool",
    "NotEnoughFunds",
    "FeeTooLow",
    "SignatureValidation",
    "BadTransactionVersion",
    "BadAddressVersionByte",
    "NoSuchContract",
    "NoSuchPublicFunction",
    "BadFunctionArgument",
    "DeserializationFailure",
    "EstimatorError",
)

# Overpay flag threshold, mirroring stellar.py's `overpaid` flag: accept but
# flag anything >2x the quote. The small under-tolerance absorbs FX drift
# between the 402 quote and verification.
_OVERPAY_FLAG_FACTOR = Decimal("2")
_UNDERPAY_TOLERANCE = Decimal("0.98")
# Below this the tolerance is meaningless — 2% of a single-digit sat quote
# rounds to a whole sat or more, so the exact amount is required instead.
_TOLERANCE_MIN_SATS = 50
_TOLERANCE_MIN_USTX = 1_000
STX_ASSET = "STX"   # the accepts[] asset id x402-stacks, AIBTC and stx402.com use for native STX

# A SIP-010 transfer's args nest one level at most (an optional memo); the
# bound exists so a hostile payload cannot recurse the decoder to death.
_MAX_CLARITY_DEPTH = 8

# SIP-005 wire constants needed for DECODING (the SDK's _stacks_tx owns the
# encoding side; these mirror it — see that module's serializer for the spec
# references).
_TX_VERSION_TO_NETWORK = {0x00: "mainnet", 0x80: "testnet"}
_AUTH_STANDARD, _AUTH_SPONSORED = 0x04, 0x05
_HASH_MODE_P2PKH = 0x00
_SPENDING_CONDITION_LEN = 1 + 20 + 8 + 8 + 1 + 65
_ADDR_VERSION_P2PKH = {"mainnet": 22, "testnet": 26}

_PC_TYPE_STX, _PC_TYPE_FUNGIBLE = 0x00, 0x01
_PC_PRINCIPAL_ORIGIN, _PC_PRINCIPAL_STANDARD, _PC_PRINCIPAL_CONTRACT = 0x01, 0x02, 0x03
_FT_SENT_EQ = 0x01

_PAYLOAD_TOKEN_TRANSFER, _PAYLOAD_CONTRACT_CALL = 0x00, 0x02
_MEMO_LEN = 34
_CV_INT, _CV_UINT, _CV_BUFFER = 0x00, 0x01, 0x02
_CV_TRUE, _CV_FALSE = 0x03, 0x04
_CV_PRINCIPAL_STANDARD, _CV_PRINCIPAL_CONTRACT = 0x05, 0x06
_CV_NONE, _CV_SOME = 0x09, 0x0A


# ── config helpers ────────────────────────────────────────────────────────────


def stacks_configured() -> bool:
    return bool(settings.STACKS_ENABLED and settings.STACKS_GATEWAY_ADDRESS)


def _network() -> str:
    return "mainnet" if settings.STACKS_NETWORK == "mainnet" else "testnet"


def _caip2() -> str:
    return STACKS_MAINNET_CAIP2 if _network() == "mainnet" else STACKS_TESTNET_CAIP2


def _network_label() -> str:
    """payment_logs / tx-consume network discriminator."""
    return f"stacks-{_network()}"


def _hiro_api() -> str:
    if settings.STACKS_HIRO_API:
        return settings.STACKS_HIRO_API.rstrip("/")
    return (
        "https://api.hiro.so" if _network() == "mainnet"
        else "https://api.testnet.hiro.so"
    )


def _sbtc_contract() -> str:
    if settings.STACKS_SBTC_CONTRACT:
        return settings.STACKS_SBTC_CONTRACT
    return SBTC_CONTRACT_MAINNET if _network() == "mainnet" else SBTC_CONTRACT_TESTNET


# ── USD→sats FX ──────────────────────────────────────────────────────────────
# sBTC is BTC-denominated, so a "$0.01 tool" needs a USD→BTC rate at
# 402-issuance. Source: CoinGecko /simple/price (the feed token_price already
# uses), cached briefly, with STACKS_FIXED_BTC_USD as the fallback so a
# CoinGecko blip degrades to the configured rate or omits the option rather
# than failing the 402. Rounding (ceil to the sat) lives in
# agentpay._stacks_tx.sats_from_usd, shared by both sides.

_rate_cache: dict = {"rate": None, "at": 0.0}   # {"rate": Decimal|None, "at": monotonic}
_stx_rate_cache: dict = {"rate": None, "at": 0.0}

# Single-flight guard for the background refresh: the 402 path never waits
# on CoinGecko when any cached rate exists (see _btc_usd_rate).
_rate_refresh_task: Optional[asyncio.Task] = None

# The fetch sits inside the 402 path on a cold cache, and external uptime
# probers arrive less often than the cache TTL, so this bound is what they
# measure.
_RATE_FETCH_TIMEOUT_S = 3.0


async def _fetch_btc_usd_live() -> Optional[Decimal]:
    """One bounded live CoinGecko fetch for BTC and STX; updates both caches."""
    try:
        async with httpx.AsyncClient(timeout=_RATE_FETCH_TIMEOUT_S) as client:
            resp = await client.get(
                f"{settings.COINGECKO_API_URL}/simple/price",
                params={"ids": "bitcoin,blockstack", "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()
            rate = Decimal(str(data["bitcoin"]["usd"]))
            if rate <= 0:
                raise ValueError("non-positive rate")
            _rate_cache["rate"] = rate
            _rate_cache["at"] = time.monotonic()
            try:
                stx = Decimal(str(data["blockstack"]["usd"]))
                if stx > 0:
                    _stx_rate_cache["rate"] = stx
                    _stx_rate_cache["at"] = _rate_cache["at"]
            except (KeyError, TypeError, ValueError, ArithmeticError):
                pass
            return rate
    except Exception as e:
        logger.warning(f"[STACKS] live BTC/USD fetch failed ({e})")
        return None


async def _stx_usd_rate() -> Optional[Decimal]:
    """STX/USD with the same stale-while-revalidate rule as BTC; the fetch is
    shared, so a call here never adds a second CoinGecko request."""
    global _rate_refresh_task
    now = time.monotonic()
    cached = _stx_rate_cache["rate"]
    if cached is not None and (now - _stx_rate_cache["at"]) < settings.STACKS_RATE_CACHE_S:
        return cached
    if cached is not None:
        if _rate_refresh_task is None or _rate_refresh_task.done():
            _rate_refresh_task = asyncio.create_task(_fetch_btc_usd_live())
        return cached
    await _fetch_btc_usd_live()
    if _stx_rate_cache["rate"] is not None:
        return _stx_rate_cache["rate"]
    if settings.STACKS_FIXED_STX_USD:
        try:
            return Decimal(str(settings.STACKS_FIXED_STX_USD))
        except Exception:
            pass
    return None


def ustx_from_usd(amount_usd: Decimal, stx_usd_rate: Decimal) -> int:
    """USD → µSTX, rounded up so the payer never underpays by rounding."""
    if stx_usd_rate <= 0:
        raise ValueError("non-positive rate")
    ustx = (Decimal(amount_usd) / stx_usd_rate * 1_000_000).to_integral_value(rounding=ROUND_CEILING)
    return max(int(ustx), 1)


async def stacks_stx_quote(price_usdc) -> Optional[tuple[int, Decimal]]:
    """(µSTX, rate) for a USD price, or None when unquotable."""
    rate = await _stx_usd_rate()
    if rate is None:
        return None
    try:
        return ustx_from_usd(Decimal(str(price_usdc)), rate), rate
    except Exception as e:
        logger.warning(f"[STACKS] STX quote failed for {price_usdc} USD: {e}")
        return None


async def _btc_usd_rate() -> Optional[Decimal]:
    """BTC/USD for quoting, without blocking a 402 on CoinGecko.

    Fresh cache → serve it. Stale cache → serve it and refresh in the
    background (single-flight); a stale rate only drifts the sats price,
    it cannot fail a settle because the settle verifies against the quote
    stored on the challenge. Empty cache → one bounded fetch, then
    STACKS_FIXED_BTC_USD, then None (the 402 omits the stacks option).
    """
    global _rate_refresh_task
    now = time.monotonic()
    cached = _rate_cache["rate"]
    if cached is not None and (now - _rate_cache["at"]) < settings.STACKS_RATE_CACHE_S:
        return cached
    if cached is not None:
        # Stale-while-revalidate: never make a caller wait on the network.
        if _rate_refresh_task is None or _rate_refresh_task.done():
            _rate_refresh_task = asyncio.create_task(_fetch_btc_usd_live())
        return cached
    live = await _fetch_btc_usd_live()
    if live is not None:
        return live
    if settings.STACKS_FIXED_BTC_USD:
        try:
            return Decimal(str(settings.STACKS_FIXED_BTC_USD))
        except Exception:
            pass
    return None


async def stacks_quote(price_usdc) -> Optional[tuple[int, Decimal]]:
    """(sats, rate) for a USD price, or None when unquotable. The rate is
    returned so callers can record it on the quote/receipt."""
    rate = await _btc_usd_rate()
    if rate is None:
        return None
    try:
        return sats_from_usd(Decimal(str(price_usdc)), rate), rate
    except Exception as e:
        logger.warning(f"[STACKS] quote failed for {price_usdc} USD: {e}")
        return None


async def stacks_quote_sats(price_usdc) -> Optional[int]:
    """USD→sats for a 402 offer (thin wrapper over stacks_quote).

    Returns None when unquotable — the 402 then simply omits the stacks
    option (fail-quiet: Stellar/Base remain offered)."""
    q = await stacks_quote(price_usdc)
    return None if q is None else q[0]


# ── STX fee suggestion ────────────────────────────────────────────────────────
# Hiro's estimator prices a payload; the shape of an sBTC transfer is fixed,
# so one template payload per network is enough. Cached like the BTC rate.

_fee_cache: dict = {"fee": None, "at": 0.0}
_fee_refresh_task: Optional[asyncio.Task] = None
_FEE_FETCH_TIMEOUT_S = 3.0
_FEE_ESTIMATED_LEN = 300     # bytes of a signed single-sig sBTC transfer


def _fee_template_payload() -> bytes:
    payee = settings.STACKS_GATEWAY_ADDRESS
    return serialize_transfer_payload(
        contract=_sbtc_contract(), sender=payee, recipient=payee,
        amount_sats=1, memo=b"0" * 34,
    )


async def _fetch_fee_live() -> Optional[int]:
    """Medium-tier estimate from Hiro, or None (testnet returns NoEstimateAvailable).

    Hiro returns three tiers. The fast tier swung 759 → 100,000+ µSTX within a
    day on mainnet (receipts 1 and 3 paid 100,000 and 50,850 for a $0.01
    call); the middle tier tracks what confirms in a block or two."""
    try:
        async with httpx.AsyncClient(timeout=_FEE_FETCH_TIMEOUT_S) as client:
            resp = await client.post(
                f"{_hiro_api()}/v2/fees/transaction",
                json={"transaction_payload": _fee_template_payload().hex(),
                      "estimated_len": _FEE_ESTIMATED_LEN},
            )
        if resp.status_code != 200:
            return None
        tiers = resp.json().get("estimations") or []
        fees = sorted(int(t["fee"]) for t in tiers)
        fee = fees[len(fees) // 2] if fees else 0
        if fee <= 0:
            return None
        _fee_cache["fee"] = fee
        _fee_cache["at"] = time.monotonic()
        return fee
    except Exception as e:
        logger.warning(f"[STACKS] fee estimate failed ({e})")
        return None


async def suggested_fee_microstx() -> int:
    """Fee to put on the 402: max(medium-tier estimate, STACKS_SUGGESTED_FEE_MICROSTX),
    capped at STACKS_FEE_CAP_MICROSTX. Stale-while-revalidate; never blocks
    a 402 when any estimate is cached; falls back to the configured fee."""
    global _fee_refresh_task
    floor = int(settings.STACKS_SUGGESTED_FEE_MICROSTX)
    cap = int(settings.STACKS_FEE_CAP_MICROSTX)
    if not settings.STACKS_FEE_ESTIMATE:
        return min(floor, cap)
    now = time.monotonic()
    est = _fee_cache["fee"]
    if est is None or (now - _fee_cache["at"]) >= settings.STACKS_RATE_CACHE_S:
        if est is None:
            est = await _fetch_fee_live()
        elif _fee_refresh_task is None or _fee_refresh_task.done():
            _fee_refresh_task = asyncio.create_task(_fetch_fee_live())
    return min(max(floor, est or 0), cap)


async def stacks_offer(price_usdc) -> Optional[tuple[int, Decimal, int]]:
    """(sats, rate, fee_microstx) for a 402, or None when unquotable."""
    q = await stacks_quote(price_usdc)
    if q is None:
        return None
    return q[0], q[1], await suggested_fee_microstx()


def stacks_402_option(quote: tuple[int, Decimal], price_usdc,
                      fee_microstx: Optional[int] = None,
                      stx_quote: Optional[tuple] = None) -> dict:
    """The `payment_options.stacks` block for an already-computed quote
    (docs/stacks-adapter.md §Wire contract). `stx` is informational: the
    SDK pays in sBTC; standard clients read accepts[]."""
    sats, rate = quote[0], quote[1]
    stx = ({"stx": {"amount_ustx": int(stx_quote[0]), "stx_usd_rate": str(stx_quote[1])}}
           if stx_quote else {})
    return {
        **stx,
        "scheme": "exact",
        "network": _caip2(),
        "amount_sats": int(sats),
        "amount_usdc": str(price_usdc),
        "btc_usd_rate": str(rate),
        "pay_to": settings.STACKS_GATEWAY_ADDRESS,
        "fee_microstx": int(fee_microstx if fee_microstx is not None
                            else settings.STACKS_SUGGESTED_FEE_MICROSTX),
        "asset": "sbtc",
        "header": "payment-signature: <base64(StacksPaymentPayload JSON)>",
    }


def stacks_accepts_entry(quote: tuple[int, Decimal], price_usdc,
                         payment_id: str, max_timeout_s: int) -> dict:
    """Standard x402 v2 `accepts[]` entry for the sBTC option. Clients echo
    it back as `accepted`, which is how extra.payment_id finds the challenge."""
    sats, rate = quote[0], quote[1]
    return {
        "scheme": "exact",
        "network": _caip2(),
        "amount": str(int(sats)),
        "asset": _sbtc_contract(),
        "payTo": settings.STACKS_GATEWAY_ADDRESS,
        "maxTimeoutSeconds": max(1, int(max_timeout_s)),
        "extra": {
            "payment_id": payment_id,
            "tokenType": "sBTC",
            "amount_usdc": str(price_usdc),
            "btc_usd_rate": str(rate),
        },
    }


def stacks_accepts_entry_stx(quote: tuple[int, Decimal], price_usdc,
                             payment_id: str, max_timeout_s: int) -> dict:
    """The native-STX `accepts[]` entry: asset "STX", amount in µSTX — the
    dialect x402-stacks, the AIBTC wallet and stx402.com share."""
    ustx, rate = quote[0], quote[1]
    return {
        "scheme": "exact",
        "network": _caip2(),
        "amount": str(int(ustx)),
        "asset": STX_ASSET,
        "payTo": settings.STACKS_GATEWAY_ADDRESS,
        "maxTimeoutSeconds": max(1, int(max_timeout_s)),
        "extra": {
            "payment_id": payment_id,
            "tokenType": "STX",
            "amount_usdc": str(price_usdc),
            "stx_usd_rate": str(rate),
        },
    }


def stacks_stx_offerable(price_usdc) -> bool:
    return bool(settings.STACKS_STX and settings.STACKS_STANDARD_CLIENTS
                and stacks_offerable(price_usdc))


def stacks_offerable(price_usdc) -> bool:
    """Stacks is configured and the tool is priced. $0 tools never offer a
    stacks option: free calls must never touch the signing path."""
    if not stacks_configured():
        return False
    try:
        return Decimal(str(price_usdc or "0")) > 0
    except Exception:
        return False


async def build_stacks_402_option(price_usdc, resource_url: str = "") -> Optional[dict]:
    """Quote + option in one step. None when Stacks isn't configured or
    quotable — the 402 then omits the option entirely. The route quotes
    first and stores the quote on the challenge; this wrapper is for
    callers without a challenge."""
    if not stacks_offerable(price_usdc):
        return None
    offer = await stacks_offer(price_usdc)
    if offer is None:
        return None
    return stacks_402_option(offer, price_usdc, fee_microstx=offer[2])


# ── payload + transaction decoding ───────────────────────────────────────────


def decode_payment_signature(header: str) -> tuple[Optional[dict], str]:
    """base64 `payment-signature` → payload dict, or (None, reason)."""
    try:
        raw = base64.b64decode(header + "=" * (-len(header) % 4))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None, "payload_not_an_object"
        return payload, ""
    except Exception:
        return None, "invalid_payment_signature_encoding"


def _accepted(payload: dict) -> dict:
    acc = payload.get("accepted")
    return acc if isinstance(acc, dict) else {}


def payload_network(payload: dict) -> str:
    """CAIP-2 network of a payment payload. The AgentPay SDK puts it at the
    top level; standard x402 v2 clients carry it only in `accepted`."""
    return str(payload.get("network") or _accepted(payload).get("network") or "")


def payload_asset(payload: dict) -> str:
    """"stx" when the client chose the native-STX entry, else "sbtc"."""
    asset = str(_accepted(payload).get("asset") or "").strip().lower()
    return "stx" if asset in ("stx", "stacks:1/native", "stacks:2147483648/native") else "sbtc"


def payload_signed_tx_hex(payload: dict) -> str:
    """Signed tx hex from either payload shape: the SDK's
    `payload.signedTransaction`, or the standard `payload.transaction`
    (AIBTC prefixes it with 0x)."""
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    raw = inner.get("signedTransaction") or inner.get("transaction") or ""
    raw = raw.strip() if isinstance(raw, str) else ""
    return raw[2:] if raw[:2].lower() == "0x" else raw


def payload_payment_id(payload: dict) -> tuple[str, bool]:
    """(payment_id, echoed). The SDK names the challenge at the top level;
    a standard client echoes the accepts[] entry it chose, whose
    extra.payment_id the gateway set. echoed=True marks the second case."""
    top = str(payload.get("payment_id") or "")
    if top:
        return top, False
    extra = _accepted(payload).get("extra")
    echoed = str(extra.get("payment_id") or "") if isinstance(extra, dict) else ""
    return echoed, bool(echoed)


class _Reader:
    """Bounds-checked cursor over the serialized tx. Any overrun raises
    ValueError — decode_sbtc_transfer turns that into a clean rejection."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("truncated transaction")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def uint(self, n: int) -> int:
        return int.from_bytes(self.take(n), "big")

    def lp_name(self) -> str:
        ln = self.u8()
        return self.take(ln).decode("ascii")

    def address(self) -> tuple[int, bytes]:
        version = self.u8()
        return version, self.take(20)


def _read_clarity_value(r: _Reader, depth: int = 0):
    """Minimal Clarity value decoder — exactly the types a SIP-010 transfer
    can carry as args. Unknown type prefixes reject the tx (we broadcast on
    the client's behalf; anything we can't fully parse is unsafe).

    `some` nests, so depth is bounded: a payload of consecutive 0x0a bytes would
    otherwise exhaust the interpreter stack, and RecursionError is a RuntimeError
    that the caller's ValueError handler does not catch."""
    if depth > _MAX_CLARITY_DEPTH:
        raise ValueError("clarity value nested too deeply")
    t = r.u8()
    if t == _CV_UINT:
        return ("uint", r.uint(16))
    if t == _CV_INT:
        return ("int", int.from_bytes(r.take(16), "big", signed=True))
    if t == _CV_BUFFER:
        ln = r.uint(4)
        return ("buffer", r.take(ln))
    if t in (_CV_TRUE, _CV_FALSE):
        return ("bool", t == _CV_TRUE)
    if t == _CV_PRINCIPAL_STANDARD:
        version, h160 = r.address()
        return ("principal", c32_address(version, h160))
    if t == _CV_PRINCIPAL_CONTRACT:
        version, h160 = r.address()
        name = r.lp_name()
        return ("principal", f"{c32_address(version, h160)}.{name}")
    if t == _CV_NONE:
        return ("none", None)
    if t == _CV_SOME:
        return ("some", _read_clarity_value(r, depth + 1))
    raise ValueError(f"unsupported Clarity value type 0x{t:02x}")


def decode_stacks_transfer(tx: bytes) -> dict:
    """Deserialize a signed SIP-005 transaction far enough to verify a
    payment: header, origin spending condition, post-conditions, then either
    a contract call with Clarity args (sBTC) or a native token transfer
    (STX). `payload_type` says which.

    Raises ValueError on anything malformed/unsupported — the caller maps
    that to a verification rejection (we never broadcast bytes we can't
    fully account for).
    """
    r = _Reader(tx)
    version = r.u8()
    network = _TX_VERSION_TO_NETWORK.get(version)
    if network is None:
        raise ValueError("unknown transaction version byte")
    r.take(4)  # chain id (redundant with the version byte for our purposes)
    auth_type = r.u8()
    if auth_type not in (_AUTH_STANDARD, _AUTH_SPONSORED):
        raise ValueError("unsupported auth type")
    hash_mode = r.u8()
    if hash_mode != _HASH_MODE_P2PKH:
        raise ValueError("unsupported origin hash mode (single-sig P2PKH only)")
    signer = r.take(20)
    nonce = r.uint(8)
    fee = r.uint(8)
    r.u8()       # key encoding
    r.take(65)   # signature
    sponsored = auth_type == _AUTH_SPONSORED
    if sponsored:
        r.take(_SPENDING_CONDITION_LEN)  # sponsor spending condition
    r.u8()  # anchor mode
    pc_mode = r.u8()
    pc_count = r.uint(4)
    if pc_count > 16:
        raise ValueError("unreasonable post-condition count")
    post_conditions = []
    for _ in range(pc_count):
        pc_type = r.u8()
        if pc_type not in (_PC_TYPE_STX, _PC_TYPE_FUNGIBLE):
            raise ValueError("unsupported post-condition type")
        p_type = r.u8()
        if p_type == _PC_PRINCIPAL_ORIGIN:
            pc_sender = "origin"
        elif p_type == _PC_PRINCIPAL_STANDARD:
            v, h = r.address()
            pc_sender = c32_address(v, h)
        elif p_type == _PC_PRINCIPAL_CONTRACT:
            v, h = r.address()
            pc_sender = f"{c32_address(v, h)}.{r.lp_name()}"
        else:
            raise ValueError("unknown post-condition principal type")
        if pc_type == _PC_TYPE_STX:
            asset_contract, asset_name = "", "STX"
        else:
            av, ah = r.address()
            asset_contract = f"{c32_address(av, ah)}.{r.lp_name()}"
            asset_name = r.lp_name()
        code = r.u8()
        amount = r.uint(8)
        post_conditions.append({
            "sender": pc_sender,
            "asset_contract": asset_contract,
            "asset_name": asset_name,
            "condition_code": code,
            "amount": amount,
        })

    sender_address = c32_address(_ADDR_VERSION_P2PKH[network], signer)
    payload_type = r.u8()
    if payload_type == _PAYLOAD_TOKEN_TRANSFER:
        kind, recipient = _read_clarity_value(r)
        if kind != "principal":
            raise ValueError("token transfer recipient is not a principal")
        amount = r.uint(8)
        memo = r.take(_MEMO_LEN).rstrip(b"\x00")
        if r.pos != len(tx):
            raise ValueError("trailing bytes after payload")
        return {
            "payload_type": "token_transfer",
            "network": network, "sponsored": sponsored, "sender": sender_address,
            "nonce": nonce, "fee": fee, "pc_mode": pc_mode,
            "post_conditions": post_conditions,
            "contract_id": "", "function": "", "amount": amount,
            "arg_sender": sender_address, "arg_recipient": recipient, "memo": memo,
        }
    if payload_type != _PAYLOAD_CONTRACT_CALL:
        raise ValueError("not a token transfer or contract call")
    cv, ch = r.address()
    contract_id = f"{c32_address(cv, ch)}.{r.lp_name()}"
    function = r.lp_name()
    arg_count = r.uint(4)
    if arg_count > 8:
        raise ValueError("unreasonable arg count")
    args = [_read_clarity_value(r) for _ in range(arg_count)]
    if r.pos != len(tx):
        raise ValueError("trailing bytes after payload")

    # SIP-010 transfer args: (amount uint) (sender principal)
    # (recipient principal) (memo (optional (buff 34)))
    amount = arg_sender = arg_recipient = memo = None
    if function == "transfer" and len(args) == 4:
        if args[0][0] == "uint":
            amount = args[0][1]
        if args[1][0] == "principal":
            arg_sender = args[1][1]
        if args[2][0] == "principal":
            arg_recipient = args[2][1]
        if args[3][0] == "some" and args[3][1][0] == "buffer":
            memo = args[3][1][1]

    return {
        "payload_type": "contract_call",
        "network": network,
        "sponsored": sponsored,
        "sender": sender_address,
        "nonce": nonce,
        "fee": fee,
        "pc_mode": pc_mode,
        "post_conditions": post_conditions,
        "contract_id": contract_id,
        "function": function,
        "amount": amount,
        "arg_sender": arg_sender,
        "arg_recipient": arg_recipient,
        "memo": memo,
    }


def decode_sbtc_transfer(tx: bytes) -> dict:
    """The contract-call decode only (the sBTC verify path calls this)."""
    out = decode_stacks_transfer(tx)
    if out["payload_type"] != "contract_call":
        raise ValueError("not a contract call")
    return out


# ── verification ─────────────────────────────────────────────────────────────

# Official sBTC deployments. Not _sbtc_contract(), which is overridable.
_CANONICAL_SBTC_CONTRACTS = frozenset({SBTC_CONTRACT_MAINNET, SBTC_CONTRACT_TESTNET})


# Challenge ids are UUID4 (gateway/x402.py); the SDK memo holds the first 34
# bytes. Must track the id format, or memos naming another challenge pass.
_UUID_MEMO = re.compile(
    rb"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{10,12}$")


def _looks_like_our_payment_id(memo: Optional[bytes]) -> bool:
    """True when the memo names one of our challenges (full or truncated)."""
    return bool(memo) and bool(_UUID_MEMO.match(memo))


def _fail(reason: str) -> dict:
    return {"authorized": False, "reason": reason, "txid": "",
            "sender": "", "amount_sats": 0, "overpaid": False}


def _amount_floor(expected: int, exact_below: int) -> int:
    """2% under the quote for FX drift, rounded up; exact below `exact_below`."""
    return expected if expected < exact_below else -(-expected * 98 // 100)


async def verify_stacks_payment(
    payment_header: str,
    *,
    expected_amount_sats: int,
    expected_recipient: str,
    payment_id: str,
    standard_client: bool = False,
    asset: str = "sbtc",
    expected_amount_ustx: int = 0,
) -> dict:
    """Decode + statically verify a signed-but-unbroadcast sBTC transfer, or
    a native STX transfer when asset="stx" (then expected_amount_ustx is the
    quote and the result carries amount_ustx).

    No network I/O: structure, binding, amount, post-conditions and the
    origin signature are all checked from the bytes. Same result contract
    shape as stellar/base verify:
    {"authorized", "reason", "txid", "sender", "amount_sats", "overpaid"}.
    The txid is recomputed from the signed bytes; the header's copy is
    never trusted.
    """
    payload, err = decode_payment_signature(payment_header)
    if err:
        return _fail(err)
    signed_hex = payload_signed_tx_hex(payload)
    try:
        signed_tx = bytes.fromhex(signed_hex)
        if not signed_tx:
            raise ValueError()
    except Exception:
        return _fail("missing_or_invalid_signed_transaction")

    try:
        tx = decode_stacks_transfer(signed_tx) if asset == "stx" else decode_sbtc_transfer(signed_tx)
    except (ValueError, RecursionError) as e:
        # RecursionError is caught alongside ValueError as a belt-and-braces
        # pair with the depth bound in _read_clarity_value: a malformed payload
        # is a rejection, never a 500.
        return _fail(f"malformed_stacks_tx: {e}")

    # No sponsored-relay path in M1: a client-signed sponsored tx carries only a
    # placeholder sponsor signature and can never broadcast. Refuse up front.
    if tx["sponsored"]:
        return _fail("sponsored_not_supported")

    if tx["network"] != _network():
        return _fail("wrong_network")
    if asset == "stx":
        if tx["payload_type"] != "token_transfer":
            return _fail("not_an_stx_transfer")
    elif tx["contract_id"] != _sbtc_contract():
        return _fail("wrong_contract")
    elif tx["function"] != "transfer":
        return _fail("not_a_transfer")
    if tx["amount"] is None or tx["arg_sender"] is None or tx["arg_recipient"] is None:
        return _fail("malformed_transfer_args")
    # SIP-010: tx-sender must equal the sender arg, or the contract aborts —
    # refuse rather than broadcast a guaranteed abort.
    if tx["arg_sender"] != tx["sender"]:
        return _fail("sender_mismatch")
    if tx["arg_recipient"] != expected_recipient:
        return _fail("wrong_recipient")

    # ── memo → payment_id binding ─────────────────────────────
    # The memo is payment_id encoded and cut to the (buff 34) cap — exactly
    # what the SDK puts there. A looser prefix rule would let a 1-byte memo
    # bind to any challenge id starting with that byte.
    # Standard clients bind via the echoed payment_id instead of the memo.
    if tx["memo"] and tx["memo"] == payment_id.encode("utf-8")[:34]:
        binding = "memo"
    elif standard_client and not _looks_like_our_payment_id(tx["memo"]):
        # Empty or the client's own nonce; a memo naming another of our
        # challenges falls through to the mismatch below.
        binding = "echoed_payment_id"
    elif not tx["memo"]:
        return _fail("missing_memo_binding")
    else:
        return _fail("memo_payment_id_mismatch")

    # ── amount (small drift tolerance only) ───────────────────────────────────
    # Round the floor up, and require the exact quote where 2% is sub-sat.
    # Truncating turned the allowance into 10% on a 10-sat quote and 25% on a
    # 4-sat one — and micro-priced tools quote in exactly that range.
    if asset == "stx":
        expected, unit, floor = expected_amount_ustx, "ustx", _amount_floor(
            expected_amount_ustx, _TOLERANCE_MIN_USTX)
    else:
        expected, unit, floor = expected_amount_sats, "sats", _amount_floor(
            expected_amount_sats, _TOLERANCE_MIN_SATS)
    if tx["amount"] < max(floor, 1):
        return _fail(f"underpaid: got {tx['amount']} {unit}, need {expected}")
    overpaid = Decimal(tx["amount"]) > Decimal(expected) * _OVERPAY_FLAG_FACTOR
    if overpaid:
        logger.warning(
            f"[STACKS] overpaid transfer flagged: {tx['amount']} {unit} vs "
            f"{expected} quoted (payment {payment_id[:8]}…)"
        )

    if asset == "stx":
        # The amount is fixed in the payload; a post-condition can only make
        # the tx abort, never move more. Refuse one that contradicts it.
        for pc in tx["post_conditions"]:
            if pc["asset_name"] == "STX" and pc["condition_code"] == _FT_SENT_EQ \
                    and pc["amount"] != tx["amount"]:
                return _fail("unsafe_post_conditions")
        payer_protection = "fixed_amount_transfer"
    # Deny mode with an exact-amount post-condition is required, except for
    # standard clients on the official sBTC contract, which moves exactly
    # `amount` (x402-stacks signs in allow mode). See docs/stacks-adapter.md.
    elif tx["pc_mode"] == 0x02:
        pc_ok = any(
            pc["condition_code"] == _FT_SENT_EQ
            and pc["amount"] == tx["amount"]
            and pc["asset_contract"] == _sbtc_contract()
            and pc["asset_name"] == SBTC_ASSET_NAME
            and pc["sender"] in ("origin", tx["sender"])
            for pc in tx["post_conditions"]
        )
        if not pc_ok:
            return _fail("unsafe_post_conditions")
        payer_protection = "deny_mode_exact_amount"
    elif (tx["pc_mode"] == 0x01 and standard_client
          and tx["contract_id"] in _CANONICAL_SBTC_CONTRACTS):
        payer_protection = "none_allow_mode"
    else:
        return _fail("post_condition_mode_not_deny")

    # Last, so the structural reasons above stay specific; still before any
    # consume or broadcast, so an unsigned tx never touches the replay store.
    if not verify_origin_signature(signed_tx):
        return _fail("invalid_origin_signature")

    return {
        "authorized": True,
        "reason": "ok",
        "txid": txid_of(signed_tx),
        "sender": tx["sender"],
        "asset": asset,
        "amount_sats": tx["amount"] if asset == "sbtc" else 0,
        "amount_ustx": tx["amount"] if asset == "stx" else 0,
        "overpaid": overpaid,
        "binding": binding,
        "payer_protection": payer_protection,
    }


# ── confirmation polling ─────────────────────────────────────────────────────


async def poll_confirmation(txid: str, *, max_polls: Optional[int] = None) -> dict:
    """GET /extended/v1/tx/{txid} until success/abort/timeout.

    Returns {"status": "success" | "rejected" | "timeout", "reason": str}.
    abort_by_post_condition is the post-condition doing its job — a definitive
    rejection, not an uncertainty. not-found-yet keeps polling (broadcast
    propagation lag).
    """
    polls = max_polls if max_polls is not None else settings.STACKS_CONFIRM_MAX_POLLS
    # Hiro 302-redirects the bare-hex form to 0x…; ask for 0x directly.
    url = f"{_hiro_api()}/extended/v1/tx/0x{txid.removeprefix('0x')}"
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for attempt in range(max(polls, 1)):
            if attempt:
                await asyncio.sleep(settings.STACKS_CONFIRM_POLL_S)
            try:
                resp = await client.get(url)
            except Exception as e:
                logger.warning(f"[STACKS] confirm poll error for {txid[:16]}…: {e}")
                continue
            if resp.status_code == 404:
                continue  # not indexed yet
            try:
                status = str(resp.json().get("tx_status", ""))
            except Exception:
                continue
            if status == "success":
                return {"status": "success", "reason": "ok"}
            if status.startswith("abort_"):
                return {"status": "rejected", "reason": status}
            # dropped_* (mempool eviction) is not definitive: the signed
            # bytes are still valid and can be re-broadcast, so keep polling
            # and let the caller end in "uncertain".
            # "pending" (or unknown) → keep polling
    return {"status": "timeout", "reason": "confirmation_timeout"}


# ── settlement ───────────────────────────────────────────────────────────────


async def _broadcast_direct(signed_tx: bytes, txid: str) -> dict:
    """Direct `POST /v2/transactions` on Hiro.

    Returns {"outcome": "accepted" | "rejected" | "uncertain", "reason": str}.
    A same-txid re-broadcast is node-level idempotent: "already in mempool"
    counts as accepted.

    The body is JSON hex, not application/octet-stream: Hiro's API mangles
    binary bodies (bytes >= 0x80 arrive as U+FFFD), and the node accepts
    both forms.
    """
    url = f"{_hiro_api()}/v2/transactions"
    try:
        async with httpx.AsyncClient(timeout=settings.STACKS_SETTLE_TIMEOUT_S) as client:
            resp = await client.post(
                url, json={"tx": signed_tx.hex()},
                headers={"Content-Type": "application/json"},
            )
    except Exception as e:
        return {"outcome": "uncertain", "reason": f"broadcast_error: {str(e)[:120]}"}

    if resp.status_code == 200:
        return {"outcome": "accepted", "reason": "ok"}

    reason_code, full = "", ""
    try:
        body = resp.json()
        reason_code = str(body.get("reason", ""))
        full = json.dumps(body)[:300]
    except Exception:
        full = resp.text[:300]

    low = full.lower()
    if reason_code not in _DEFINITIVE_REJECTIONS and "already" in low and (
        "mempool" in low or "chain" in low
    ):
        # "transaction already exists" phrasing means OUR txid is known —
        # treat as accepted and poll. (ConflictingNonceInMempool is a
        # DIFFERENT tx holding our nonce; that one stays a rejection.)
        return {"outcome": "accepted", "reason": "already_known"}
    if reason_code in _DEFINITIVE_REJECTIONS:
        return {"outcome": "rejected", "reason": f"broadcast rejected: {reason_code}"}
    if resp.status_code == 400:
        # Unknown 400 shape: the node refused it — a 400 never broadcasts.
        return {"outcome": "rejected", "reason": f"broadcast rejected: {full[:160]}"}
    return {"outcome": "uncertain", "reason": f"broadcast_http_{resp.status_code}"}


async def _settle_via_facilitator(payment_payload: dict, requirements: dict) -> dict:
    """POST {STACKS_FACILITATOR_URL}/settle (x402 v2 shape).

    Returns {"outcome": "ok" | "rejected" | "unavailable" | "uncertain",
             "reason": str}. Anything transport-shaped is "unavailable" —
    the caller degrades to direct broadcast (facilitator posture: the young
    facilitator stacks are convenience, never a hard dependency).
    """
    url = settings.STACKS_FACILITATOR_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=settings.STACKS_SETTLE_TIMEOUT_S) as client:
            resp = await client.post(
                f"{url}/settle",
                json={
                    "x402Version": 2,
                    "paymentPayload": payment_payload,
                    "paymentRequirements": requirements,
                },
            )
    except Exception as e:
        return {"outcome": "unavailable",
                "reason": f"facilitator_unreachable: {str(e)[:120]}"}

    if resp.status_code >= 500:
        return {"outcome": "unavailable", "reason": f"facilitator_http_{resp.status_code}"}
    try:
        data = resp.json()
    except Exception:
        return {"outcome": "unavailable", "reason": "facilitator_bad_body"}

    if resp.status_code == 200 and data.get("success"):
        return {"outcome": "ok", "reason": "ok"}

    err = str(data.get("errorReason") or data.get("reason") or data.get("error") or "")
    if any(code in err for code in _DEFINITIVE_REJECTIONS):
        return {"outcome": "rejected", "reason": f"broadcast rejected: {err[:160]}"}
    # Ambiguous failure (timeout waiting for confirmation, unknown error):
    # the facilitator may have broadcast. Caller polls, then direct-broadcasts.
    return {"outcome": "uncertain",
            "reason": err[:160] or f"facilitator_http_{resp.status_code}"}


async def settle_stacks_payment(
    signed_tx: bytes,
    txid: str,
    *,
    payment_id: str,
    payment_payload: Optional[dict] = None,
    requirements: Optional[dict] = None,
) -> dict:
    """Consume the txid, then broadcast + confirm under one deadline.

    Step 1 here: consume `txid` (in-memory check-and-add + awaited Supabase
    insert, fail-closed on infra error) before any broadcast — a replayed
    txid dies here. `txid` is recomputed server-side from `signed_tx` by the
    caller, never taken from the header.
    Steps 2–5 in `_broadcast_and_confirm`, bounded by STACKS_SETTLE_DEADLINE_S.

    Returns {"ok", "state": "ok"|"ok_recovered"|"rejected"|"uncertain",
             "txid", "reason"}.
    """
    label = _network_label()

    # ── consume the txid before broadcast ─────────────────────────────────
    if txid in _used_stacks_txids:
        return {"ok": False, "state": "rejected", "txid": txid,
                "reason": "replay_attack"}
    _remember_txid(txid)
    if sb.sb_enabled():
        recorded = await sb.record_tx_hash(txid, label)
        if recorded is False:
            return {"ok": False, "state": "rejected", "txid": txid,
                    "reason": "replay_attack"}
        if recorded is None:
            # Durable consume unconfirmed: broadcasting now would make this
            # payment replayable after a restart, so nothing is broadcast.
            # The proof cannot be retried as-is (its payment_id is already
            # consumed upstream), so answer "rejected": the SDK confirms on
            # Hiro that the tx is absent, zeroes the leg, and signs again
            # against a fresh 402.
            _used_stacks_txids.pop(txid, None)
            return {"ok": False, "state": "rejected", "txid": txid,
                    "reason": ("replay_store_unavailable: nothing was broadcast "
                               "— request a fresh 402 and sign again")}

    # ── steps 2–5 under one wall-clock deadline: the edge cuts the request
    # at 100s with no body, which would strip txid/payment_status from the
    # SDK's reply. Cutting to "uncertain" ourselves keeps the structured
    # reply, and redemption finishes the call later.
    try:
        return await asyncio.wait_for(
            _broadcast_and_confirm(signed_tx, txid, payment_payload, requirements),
            timeout=settings.STACKS_SETTLE_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[STACKS] settle deadline ({settings.STACKS_SETTLE_DEADLINE_S:.0f}s) "
                       f"hit for {txid[:20]}… — answering uncertain")
        return {"ok": False, "state": "uncertain", "txid": txid,
                "reason": "settle_deadline_pending_confirmation"}


async def _broadcast_and_confirm(
    signed_tx: bytes, txid: str,
    payment_payload: Optional[dict], requirements: Optional[dict],
) -> dict:
    """Steps 2–5 of the settle; the caller bounds the wall time.

      2. Facilitator /settle when STACKS_FACILITATOR_URL is set.
      3. Facilitator down/5xx/unreachable → direct Hiro broadcast.
      4. Ambiguous outcome after any broadcast → poll_confirmation; confirmed ⇒
         "ok_recovered" (same-txid re-broadcast is node-idempotent).
      5. Definitive node rejection ⇒ "rejected" (the consume stays; the SDK
         re-signs with a fresh nonce, producing a new txid).
    """
    broadcast_attempted = False
    if settings.STACKS_FACILITATOR_URL and payment_payload is not None:
        fac = await _settle_via_facilitator(payment_payload, requirements or {})
        if fac["outcome"] == "ok":
            confirm = await poll_confirmation(txid)
            if confirm["status"] == "success":
                return {"ok": True, "state": "ok", "txid": txid, "reason": "ok"}
            if confirm["status"] == "rejected":
                return {"ok": False, "state": "rejected", "txid": txid,
                        "reason": confirm["reason"]}
            # Facilitator said ok but we can't see it confirmed — uncertain;
            # never claim settled without proof either way.
            return {"ok": False, "state": "uncertain", "txid": txid,
                    "reason": "facilitator_ok_unconfirmed"}
        if fac["outcome"] == "rejected":
            return {"ok": False, "state": "rejected", "txid": txid,
                    "reason": fac["reason"]}
        if fac["outcome"] == "uncertain":
            broadcast_attempted = True
            # The facilitator may have broadcast before failing — check the
            # chain before re-broadcasting.
            confirm = await poll_confirmation(
                txid, max_polls=max(settings.STACKS_CONFIRM_MAX_POLLS // 2, 2)
            )
            if confirm["status"] == "success":
                logger.info(f"[STACKS] settle RECOVERED (facilitator ambiguous, "
                            f"tx confirmed): {txid[:20]}…")
                return {"ok": True, "state": "ok_recovered", "txid": txid,
                        "reason": "ok_recovered"}
            if confirm["status"] == "rejected":
                return {"ok": False, "state": "rejected", "txid": txid,
                        "reason": confirm["reason"]}
        # "unavailable" (or uncertain + unconfirmed) → degrade to direct.
        logger.warning(f"[STACKS] facilitator degraded ({fac['reason']}) — "
                       f"direct Hiro broadcast for {txid[:20]}…")

    direct = await _broadcast_direct(signed_tx, txid)
    if direct["outcome"] == "rejected":
        if broadcast_attempted:
            # A rejected re-broadcast after an ambiguous facilitator attempt
            # can mean the first broadcast is live (e.g. our own tx now holds
            # the nonce in the mempool) — poll once more before answering.
            confirm = await poll_confirmation(txid)
            if confirm["status"] == "success":
                return {"ok": True, "state": "ok_recovered", "txid": txid,
                        "reason": "ok_recovered"}
            if confirm["status"] == "timeout":
                return {"ok": False, "state": "uncertain", "txid": txid,
                        "reason": f"rebroadcast_rejected_after_ambiguous: {direct['reason']}"}
        return {"ok": False, "state": "rejected", "txid": txid,
                "reason": direct["reason"]}
    if direct["outcome"] == "uncertain" and not broadcast_attempted:
        # Transport failure before any known broadcast — the tx may or may
        # not have reached the node. Poll; confirmed ⇒ ok_recovered.
        confirm = await poll_confirmation(txid)
        if confirm["status"] == "success":
            return {"ok": True, "state": "ok_recovered", "txid": txid,
                    "reason": "ok_recovered"}
        if confirm["status"] == "rejected":
            return {"ok": False, "state": "rejected", "txid": txid,
                    "reason": confirm["reason"]}
        return {"ok": False, "state": "uncertain", "txid": txid,
                "reason": direct["reason"]}

    # accepted (directly, or after an ambiguous prior attempt) → confirm.
    confirm = await poll_confirmation(txid)
    if confirm["status"] == "success":
        state = "ok_recovered" if broadcast_attempted else "ok"
        return {"ok": True, "state": state, "txid": txid, "reason": confirm["reason"]
                if state == "ok" else "ok_recovered"}
    if confirm["status"] == "rejected":
        # abort_by_post_condition / abort_by_response: mined and aborted —
        # the post-condition did its job. Definitive.
        return {"ok": False, "state": "rejected", "txid": txid,
                "reason": confirm["reason"]}
    return {"ok": False, "state": "uncertain", "txid": txid,
            "reason": "broadcast_accepted_pending_confirmation"}
