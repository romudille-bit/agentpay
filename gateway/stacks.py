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
import time
from decimal import Decimal
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
    txid_of,
)
from gateway.config import settings
from gateway.services import supabase as sb

logger = logging.getLogger("gateway.stacks")

__all__ = [
    "verify_stacks_payment",
    "settle_stacks_payment",
    "poll_confirmation",
    "decode_sbtc_transfer",
    "decode_payment_signature",
    "build_stacks_402_option",
    "stacks_quote_sats",
    "stacks_configured",
]

# In-memory fast guard for txid consumption (mirrors _used_base_tx_hashes in
# gateway/base.py — single-process guard when Supabase is disabled/unreachable).
_used_stacks_txids: set[str] = set()

# Node rejection reasons that are DEFINITIVE — the tx was refused at
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
# flag anything >2x the quote. Small under-tolerance absorbs FX drift between
# the 402 quote and verification (AGE-24 owns the real FX; with the M1 fixed
# rate the two are identical, so this only matters once live rates land).
_OVERPAY_FLAG_FACTOR = Decimal("2")
_UNDERPAY_TOLERANCE = Decimal("0.98")

# SIP-005 wire constants needed for DECODING (the SDK's _stacks_tx owns the
# encoding side; these mirror it — see that module's serializer for the spec
# references).
_TX_VERSION_TO_NETWORK = {0x00: "mainnet", 0x80: "testnet"}
_AUTH_STANDARD, _AUTH_SPONSORED = 0x04, 0x05
_HASH_MODE_P2PKH = 0x00
_SPENDING_CONDITION_LEN = 1 + 20 + 8 + 8 + 1 + 65
_ADDR_VERSION_P2PKH = {"mainnet": 22, "testnet": 26}

_PC_TYPE_FUNGIBLE = 0x01
_PC_PRINCIPAL_ORIGIN, _PC_PRINCIPAL_STANDARD, _PC_PRINCIPAL_CONTRACT = 0x01, 0x02, 0x03
_FT_SENT_EQ = 0x01

_PAYLOAD_CONTRACT_CALL = 0x02
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


# ── USD→sats FX (AGE-24) ──────────────────────────────────────────────────────
# sBTC is BTC-denominated (sats, 8 decimals), so a "$0.01 tool" needs a
# USD→BTC rate at 402-issuance. Rate source: CoinGecko /simple/price (the same
# feed token_price uses — keyless, no new dependency), cached briefly, with
# STACKS_FIXED_BTC_USD as the fallback floor so a CoinGecko blip never hard-
# fails 402 issuance (it degrades to the configured rate, or omits the option).
#
# The rounding rule (ceil to the sat — never quote fewer sats than the USD
# price) lives in agentpay._stacks_tx.sats_from_usd, shared by both sides.

_rate_cache: dict = {"rate": None, "at": 0.0}   # {"rate": Decimal|None, "at": monotonic}

# Per-payment quote store: the sats quoted at 402-ISSUANCE are authoritative
# for that payment. Settle verifies against THIS, not a fresh re-quote, so a
# BTC move between issuance and settle can't spuriously fail the amount check.
# The validity window is the challenge's own expiry (settle already rejects an
# expired challenge). In-memory + single-process, exactly like x402's
# _pending_challenges — fine for the 402→settle window; durable per-payment
# rate columns in payment_logs are the M2 auditability follow-up.
_stacks_quotes: dict[str, dict] = {}   # payment_id → {"sats", "rate", "quoted_at"}
_QUOTE_STORE_MAX = 5000
_QUOTE_STORE_TTL_S = 3600


# AGE-135: single-flight guard for the background rate refresh. The 402 path
# must never block on CoinGecko when ANY cached rate exists — see _btc_usd_rate.
_rate_refresh_task: Optional[asyncio.Task] = None

# Bounded fetch: the old 10s timeout sat INSIDE the 402 challenge path and,
# with probes arriving less often than the 60s cache TTL, every external
# prober hit a cold cache → paid-tool 402s carried a live CoinGecko
# round-trip. fuchss's 90d histories read pre_trade_check at 97.94%
# trailing-30d availability (timeout-class failures), vs 99.36% for
# session_create whose 402 has no stacks leg. 3s is generous for CoinGecko's
# p99 and keeps the worst-case cold-boot 402 well under prober timeouts.
_RATE_FETCH_TIMEOUT_S = 3.0


async def _fetch_btc_usd_live() -> Optional[Decimal]:
    """One bounded live CoinGecko fetch; updates the cache on success."""
    try:
        async with httpx.AsyncClient(timeout=_RATE_FETCH_TIMEOUT_S) as client:
            resp = await client.get(
                f"{settings.COINGECKO_API_URL}/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            usd = resp.json()["bitcoin"]["usd"]
            rate = Decimal(str(usd))
            if rate <= 0:
                raise ValueError("non-positive rate")
            _rate_cache["rate"] = rate
            _rate_cache["at"] = time.monotonic()
            return rate
    except Exception as e:
        logger.warning(f"[STACKS] live BTC/USD fetch failed ({e})")
        return None


async def _btc_usd_rate() -> Optional[Decimal]:
    """BTC/USD for quoting, without ever blocking a 402 on CoinGecko (AGE-135).

    Fresh cache (< STACKS_RATE_CACHE_S) → serve it.
    Stale cache → serve the stale value IMMEDIATELY and refresh in the
      background (single-flight). Staleness is safe: the quote binds at
      402-issuance and settle verifies against the stored quote
      (_stacks_quotes), so a stale rate only drifts the sats price slightly —
      it can never fail a settle.
    Empty cache (cold boot) → one bounded (3s) blocking fetch, then
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


def _remember_quote(payment_id: str, sats: int, rate: Decimal) -> None:
    if len(_stacks_quotes) >= _QUOTE_STORE_MAX:
        cutoff = time.time() - _QUOTE_STORE_TTL_S
        for pid in [p for p, q in _stacks_quotes.items() if q["quoted_at"] < cutoff]:
            _stacks_quotes.pop(pid, None)
    _stacks_quotes[payment_id] = {
        "sats": sats, "rate": str(rate), "quoted_at": time.time(),
    }


def stacks_quoted_sats(payment_id: str) -> Optional[dict]:
    """The quote recorded at 402-issuance for this payment, or None if it was
    never stored / was swept (restart, multi-worker, GET-issued 402). Settle
    prefers this over re-quoting."""
    return _stacks_quotes.get(payment_id)


def forget_stacks_quote(payment_id: str) -> None:
    """Drop a stored quote once its payment reaches a terminal settle state."""
    _stacks_quotes.pop(payment_id, None)


async def build_stacks_402_option(
    price_usdc, resource_url: str = "", *, payment_id: Optional[str] = None,
) -> Optional[dict]:
    """The `payment_options.stacks` block of AgentPay's native 402
    (docs/stacks-adapter.md §Wire contract). None when Stacks isn't
    configured/quotable — the 402 then omits the option entirely.

    When `payment_id` is given, the quoted sats + rate are stored so settle
    verifies against THIS quote rather than re-quoting (FX-drift safety).

    $0 tools never offer a stacks option: free calls must never touch the
    signing path (the SDK's free:<id> proof flow handles them chain-free).
    """
    if not stacks_configured():
        return None
    try:
        if Decimal(str(price_usdc or "0")) == 0:
            return None
    except Exception:
        return None
    q = await stacks_quote(price_usdc)
    if q is None:
        return None
    sats, rate = q
    if payment_id:
        _remember_quote(payment_id, sats, rate)
    return {
        "scheme": "exact",
        "network": _caip2(),
        "amount_sats": sats,
        "amount_usdc": str(price_usdc),
        "btc_usd_rate": str(rate),   # transparency: the rate this quote used
        "pay_to": settings.STACKS_GATEWAY_ADDRESS,
        "fee_microstx": settings.STACKS_SUGGESTED_FEE_MICROSTX,
        "asset": "sbtc",
        "header": "payment-signature: <base64(StacksPaymentPayload JSON)>",
    }


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


def _read_clarity_value(r: _Reader):
    """Minimal Clarity value decoder — exactly the types a SIP-010 transfer
    can carry as args. Unknown type prefixes reject the tx (we broadcast on
    the client's behalf; anything we can't fully parse is unsafe)."""
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
        return ("some", _read_clarity_value(r))
    raise ValueError(f"unsupported Clarity value type 0x{t:02x}")


def decode_sbtc_transfer(tx: bytes) -> dict:
    """Deserialize a signed SIP-005 contract-call transaction far enough to
    verify an sBTC transfer: header, origin spending condition, post-
    conditions, and the contract-call payload with Clarity args.

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
        if pc_type != _PC_TYPE_FUNGIBLE:
            # STX / NFT post-conditions never appear on our transfers.
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

    payload_type = r.u8()
    if payload_type != _PAYLOAD_CONTRACT_CALL:
        raise ValueError("not a contract call")
    cv, ch = r.address()
    contract_id = f"{c32_address(cv, ch)}.{r.lp_name()}"
    function = r.lp_name()
    arg_count = r.uint(4)
    if arg_count > 8:
        raise ValueError("unreasonable arg count")
    args = [_read_clarity_value(r) for _ in range(arg_count)]
    if r.pos != len(tx):
        raise ValueError("trailing bytes after payload")

    sender_address = c32_address(_ADDR_VERSION_P2PKH[network], signer)

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


# ── verification ─────────────────────────────────────────────────────────────


def _fail(reason: str) -> dict:
    return {"authorized": False, "reason": reason, "txid": "",
            "sender": "", "amount_sats": 0, "overpaid": False}


async def verify_stacks_payment(
    payment_header: str,
    *,
    expected_amount_sats: int,
    expected_recipient: str,
    payment_id: str,
) -> dict:
    """Decode + statically verify a signed-but-unbroadcast sBTC transfer.

    NO network I/O: everything here is checkable from the bytes. Same result
    contract shape as stellar/base verify:
    {"authorized", "reason", "txid", "sender", "amount_sats", "overpaid"}.
    The txid is RECOMPUTED from the signed bytes — the header's copy is
    never trusted (wire contract).
    """
    payload, err = decode_payment_signature(payment_header)
    if err:
        return _fail(err)
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    signed_hex = inner.get("signedTransaction") or ""
    try:
        signed_tx = bytes.fromhex(signed_hex)
        if not signed_tx:
            raise ValueError()
    except Exception:
        return _fail("missing_or_invalid_signed_transaction")

    try:
        tx = decode_sbtc_transfer(signed_tx)
    except ValueError as e:
        return _fail(f"malformed_stacks_tx: {e}")

    # No sponsored-relay path in M1: a client-signed sponsored tx carries only a
    # placeholder sponsor signature and can never broadcast. Refuse up front.
    if tx["sponsored"]:
        return _fail("sponsored_not_supported")

    if tx["network"] != _network():
        return _fail("wrong_network")
    if tx["contract_id"] != _sbtc_contract():
        return _fail("wrong_contract")
    if tx["function"] != "transfer":
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
    # The memo carries payment_id[:34] (the SIP-010 buff cap truncates UUIDs);
    # prefix rule both ways, mirroring the Stellar memo match.
    if not tx["memo"]:
        return _fail("missing_memo_binding")
    try:
        memo_str = tx["memo"].decode("utf-8")
    except Exception:
        return _fail("undecodable_memo")
    if not (payment_id.startswith(memo_str) or memo_str.startswith(payment_id)):
        return _fail("memo_payment_id_mismatch")

    # ── amount (AGE-24 owns the FX; small drift tolerance only) ──────────────
    floor_sats = int(Decimal(expected_amount_sats) * _UNDERPAY_TOLERANCE)
    if tx["amount"] < max(floor_sats, 1):
        return _fail(
            f"underpaid: got {tx['amount']} sats, need {expected_amount_sats}"
        )
    overpaid = Decimal(tx["amount"]) > Decimal(expected_amount_sats) * _OVERPAY_FLAG_FACTOR
    if overpaid:
        logger.warning(
            f"[STACKS] overpaid transfer flagged: {tx['amount']} sats vs "
            f"{expected_amount_sats} quoted (payment {payment_id[:8]}…)"
        )

    # Deny-mode (0x02) only: the tx must abort on any post-condition it does
    # not list. The SDK always builds deny-mode; allow-mode is refused.
    if tx["pc_mode"] != 0x02:
        return _fail("post_condition_mode_not_deny")

    # ── mandatory post-condition: exactly-N-sats-leave-sender ────────────────
    # This is what makes broadcasting a stranger's signed tx safe; a transfer
    # without it (or with a weaker code) is refused, never repaired.
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

    return {
        "authorized": True,
        "reason": "ok",
        "txid": txid_of(signed_tx),
        "sender": tx["sender"],
        "amount_sats": tx["amount"],
        "overpaid": overpaid,
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
    url = f"{_hiro_api()}/extended/v1/tx/{txid}"
    async with httpx.AsyncClient(timeout=10.0) as client:
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
            if status.startswith("abort_") or status.startswith("dropped_"):
                return {"status": "rejected", "reason": status}
            # "pending" (or unknown) → keep polling
    return {"status": "timeout", "reason": "confirmation_timeout"}


# ── settlement ───────────────────────────────────────────────────────────────


async def _broadcast_direct(signed_tx: bytes, txid: str) -> dict:
    """Direct `POST /v2/transactions` on Hiro.

    Returns {"outcome": "accepted" | "rejected" | "uncertain", "reason": str}.
    A same-txid re-broadcast is node-level idempotent: "already in mempool"
    counts as accepted.

    Body is JSON hex rather than application/octet-stream. As of 2026-08-15 the
    Hiro testnet API corrupts binary bodies: bytes >= 0x80 arrive as U+FFFD
    (ef bf bd), so the node reads 0xbd where the auth flags belong and rejects
    with "unrecognized auth flags 189". It rejects Hiro's own previously-mined
    faucet transaction identically, so this is not specific to transactions we
    build. JSON carries the same bytes as ASCII hex, survives the transcoding,
    and is equally supported by the node.
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
    """Broadcast + confirm. The order is the security model:

      1. Consume `txid` (in-memory check-and-add + awaited Supabase insert,
         fail-closed on infra error) before any broadcast — a replayed txid
         dies here. `txid` is recomputed server-side from `signed_tx` by the
         caller, never taken from the header.
      2. Facilitator /settle when STACKS_FACILITATOR_URL is set.
      3. Facilitator down/5xx/unreachable → direct Hiro broadcast.
      4. Ambiguous outcome after any broadcast → poll_confirmation; confirmed ⇒
         "ok_recovered" (never charge-for-nothing; same-txid re-broadcast is
         node-idempotent).
      5. Definitive node rejection ⇒ "rejected" (the consume stays; the SDK
         re-signs with a fresh nonce, producing a new txid).

    Returns {"ok", "state": "ok"|"ok_recovered"|"rejected"|"uncertain",
             "txid", "reason"}.
    """
    label = _network_label()

    # ── consume the txid before broadcast — a replay must die here ────────
    if txid in _used_stacks_txids:
        return {"ok": False, "state": "rejected", "txid": txid,
                "reason": "replay_attack"}
    _used_stacks_txids.add(txid)
    if sb.sb_enabled():
        recorded = await sb.record_tx_hash(txid, label)
        if recorded is False:
            return {"ok": False, "state": "rejected", "txid": txid,
                    "reason": "replay_attack"}
        if recorded is None:
            # Durable consume unconfirmed (AGE-60 pattern): broadcasting now
            # would make this payment replayable after a restart. Release the
            # in-memory hold so the SAME proof can retry once the store is
            # back. Retryable — deliberately NOT "rejected" (nothing was
            # refused by a node; the SDK must keep the leg intact).
            _used_stacks_txids.discard(txid)
            return {"ok": False, "state": "uncertain", "txid": txid,
                    "reason": ("replay_check_unavailable: durable replay store "
                               "unreachable — retry the same proof")}

    # ── 2./3. broadcast: facilitator first, direct Hiro as the fallback ──────
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
            # chain BEFORE re-broadcasting (the ok_recovered lesson).
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
            # can mean the FIRST broadcast is live (e.g. our own tx now holds
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
