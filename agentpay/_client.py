"""
_client.py — HTTP client that handles the x402 payment flow.

Internal helper used by Session.call(). Not part of the public API.
"""

import httpx
import logging
from decimal import Decimal

from agentpay._wallet import AgentWallet, PaymentFailed, RefundPending

logger = logging.getLogger(__name__)


class AgentPayClient:
    """
    HTTP client that handles the full x402 payment flow.

    When a tool returns 402:
      1. Reads payment details from response
      2. Sends USDC via Stellar
      3. Retries with payment proof header
    """

    def __init__(self, wallet: AgentWallet, gateway_url: str):
        self.wallet = wallet
        self.gateway_url = gateway_url
        self.call_log: list[dict] = []

    def _settle_base(self, client, url, payload, base_opt, challenge):
        """
        Settle a paid AgentPay tool on Base via the gasless EIP-3009 (Mode A)
        path and return the retry HTTP response.

        `base_opt` is the `payment_options.base` block from AgentPay's native
        402. Nothing is broadcast client-side — the gateway's CDP facilitator
        settles the signed authorization only if it accepts the retry, so a
        rejected call moves no USDC.
        """
        accept = {
            "amount":            str(base_opt.get("amount_atomic") or base_opt.get("amount")),
            "asset":             base_opt.get("asset"),
            "payTo":             base_opt.get("pay_to") or base_opt.get("payTo"),
            "network":           base_opt.get("network", "eip155:8453"),
            "scheme":            base_opt.get("scheme", "exact"),
            "maxTimeoutSeconds": int(base_opt.get("maxTimeoutSeconds", 300)),
        }
        logger.info(
            f"  Settling on Base (EIP-3009, gasless) "
            f"{base_opt.get('amount_usdc')} USDC → {str(accept['payTo'])[:10]}..."
        )
        sig = self.wallet.build_base_payment_signature(accept, url)
        # PAYMENT-SIGNATURE only. Sending the same payload in X-PAYMENT (the
        # x402 standard header) collides with the gateway's legacy Stellar
        # X-Payment header and got every Mode A named-tool call rejected with
        # 'Invalid X-Payment header format'. This path only talks to AgentPay's
        # own gateway; external x402 URLs go through _call_x402_url instead.
        return client.post(
            url,
            json=payload,
            headers={
                "PAYMENT-SIGNATURE": sig,
                "X-Agent-Address":   self.wallet.base_address,
            },
        )

    def call_tool(
        self,
        tool_name: str,
        parameters: dict,
        max_spend: str = None,
        *,
        prefer_chain: str = "base",
        chain_is_explicit: bool = False,
    ) -> dict:
        """
        Call a paid tool. Handles 402 automatically.
        Raises ValueError if max_spend is set and price exceeds it.

        Chain selection for PAID tools:
          - prefer_chain="base" (default) settles via the gateway's Base/EIP-3009
            (Mode A) path when the wallet has a Base key and the 402 advertises a
            Base option — this is the path that keeps AgentPay's listing live on
            Bazaar. Stellar is used as the automatic fallback otherwise.
          - prefer_chain="stellar" forces the legacy Stellar settlement.
          - chain_is_explicit=True means the caller demanded this chain; if it
            isn't usable a PaymentFailed is raised instead of falling back.
        Free ($0) tools never settle on-chain and ignore prefer_chain entirely.
        """
        prefer_chain = (prefer_chain or "base").lower()
        url = f"{self.gateway_url}/tools/{tool_name}/call"
        payload = {"parameters": parameters, "agent_address": self.wallet.public_key}

        with httpx.Client(timeout=60.0) as client:

            # ── First request — no payment ─────────────────────────────────
            logger.info(f"→ Calling: {tool_name} | params: {parameters}")
            resp = client.post(url, json=payload)

            if resp.status_code == 200:
                # Free tool — the gateway returns 200 directly with no 402.
                # Record it anyway (at $0) so it appears in the session receipt.
                # Full session visibility means every call shows up, free or paid.
                self.call_log.append({
                    "tool": tool_name,
                    "amount_usdc": "0",
                    "tx_hash": None,
                    "success": True,
                    "free": True,
                })
                logger.info(f"  ✓ {tool_name} (free) — logged at $0")
                return resp.json()

            if resp.status_code != 402:
                raise Exception(f"Unexpected {resp.status_code}: {resp.text}")

            # ── 402 received — parse payment details ───────────────────────
            data = resp.json()
            payment_id  = data["payment_id"]
            amount_usdc = data["amount_usdc"]
            pay_to      = data["pay_to"]

            logger.info(f"  402 — pay {amount_usdc} USDC to {pay_to[:12]}...")

            # Optional per-call spend cap
            if max_spend and Decimal(str(amount_usdc)) > Decimal(str(max_spend)):
                raise ValueError(
                    f"Tool '{tool_name}' costs {amount_usdc} USDC "
                    f"which exceeds max_spend={max_spend}"
                )

            # ── Free tool ($0 challenge): no on-chain settlement ───────────
            # The gateway issues a 402 for free tools too (so every call gets
            # a payment_logs row + receipt), but there is nothing to pay. Skip
            # wallet.pay — a $0 Stellar settlement would fail on an unfunded
            # account — and retry with a unique free proof. The tx_hash is
            # derived from the (unique) payment_id so it never collides with a
            # prior free call's replay record.
            if Decimal(str(amount_usdc)) == 0:
                # ── Free tool: never settle on-chain (chain pref ignored) ──
                tx_hash = f"free:{payment_id}"
                logger.info(f"  ✓ {tool_name} is free — skipping settlement")
                proof_header = (
                    f"tx_hash={tx_hash},"
                    f"from={self.wallet.public_key},"
                    f"id={payment_id}"
                )
                retry = client.post(
                    url,
                    json=payload,
                    headers={
                        "X-Payment": proof_header,
                        "X-Agent-Address": self.wallet.public_key,
                    },
                )
            else:
                # ── Paid tool: prefer Base (Mode A) → fall back to Stellar ──
                base_opt = (data.get("payment_options") or {}).get("base")
                want_base = (
                    prefer_chain != "stellar"
                    and base_opt is not None
                    and getattr(self.wallet, "base_address", None)
                )
                # Say WHY we're skipping an offered Base option instead of
                # silently degrading to Stellar (usually: missing [base]
                # extra / venv not activated / no base_key).
                if base_opt is not None and prefer_chain != "stellar" and not want_base:
                    why = (getattr(self.wallet, "base_disabled_reason", None)
                           or "no Base key configured (pass base_key= or set BASE_AGENT_KEY)")
                    logger.warning(f"  402 offers Base but settling on Stellar — {why}")
                if chain_is_explicit and prefer_chain == "base" and not want_base:
                    raise PaymentFailed(
                        f"chain='base' requested for '{tool_name}' but "
                        f"{'no Base wallet configured' if not getattr(self.wallet, 'base_address', None) else 'the gateway did not offer a Base option'}."
                    )

                retry = None
                if want_base:
                    try:
                        retry = self._settle_base(
                            client, url, payload, base_opt, data
                        )
                    except Exception as e:
                        msg = str(e)[:160]
                        if isinstance(e, ImportError):
                            msg += ' — install the Base extra: pip install "agentpay-x402[base]"'
                        if chain_is_explicit and prefer_chain == "base":
                            raise PaymentFailed(f"base settlement failed: {msg}")
                        logger.warning(
                            f"  Base settlement failed ({msg}) — falling back to Stellar"
                        )
                        retry = None

                if retry is None:
                    # ── Stellar settlement (fallback / explicit) ───────────
                    logger.info(f"  Sending payment on Stellar {self.wallet.network}...")
                    payment = self.wallet.pay(
                        destination=pay_to,
                        amount_usdc=amount_usdc,
                        memo=payment_id[:28],
                    )
                    if not payment["success"]:
                        reason = payment["reason"]
                        # Funding wall: make "underfunded" actionable by
                        # naming the agent's own fundable address(es).
                        if any(k in reason.lower() for k in
                               ("underfunded", "no_trust", "not found",
                                "not_found", "resource missing")):
                            hint = (
                                f" To use paid tools, fund {self.wallet.public_key} "
                                f"with USDC on Stellar {self.wallet.network}"
                            )
                            if getattr(self.wallet, "base_address", None):
                                hint += (
                                    f", or fund {self.wallet.base_address} "
                                    f"with USDC on Base mainnet"
                                )
                            reason += "." + hint + "."
                            disabled = getattr(self.wallet, "base_disabled_reason", None)
                            if disabled:
                                reason += f" (Base settlement unavailable: {disabled})"
                        raise PaymentFailed(reason)
                    tx_hash = payment["tx_hash"]
                    logger.info(f"  ✓ Payment sent | tx: {tx_hash[:16]}...")
                    proof_header = (
                        f"tx_hash={tx_hash},"
                        f"from={self.wallet.public_key},"
                        f"id={payment_id}"
                    )
                    retry = client.post(
                        url,
                        json=payload,
                        headers={
                            "X-Payment": proof_header,
                            "X-Agent-Address": self.wallet.public_key,
                        },
                    )
                else:
                    tx_hash = retry.headers.get("x-tx-hash", "") or ""

            if retry.status_code != 200:
                # Gateway refund contract: on tool-failure-post-verify the
                # gateway now returns 502 with a structured body carrying
                # payment_status, refund_eta_seconds, payment_id, and
                # error_reason. Surface that as a typed RefundPending so
                # callers can branch on the failure mode instead of
                # parsing JSON themselves.
                #
                # Fallback to the generic Exception if the body doesn't
                # parse as JSON (e.g. Railway edge 500s, unrelated
                # gateway errors) — preserves the previous behaviour
                # for shapes we don't recognise.
                try:
                    err_body = retry.json()
                    payment_status = err_body.get("payment_status")
                except Exception:
                    err_body = None
                    payment_status = None

                if payment_status in ("refund_pending", "refund_disabled"):
                    raise RefundPending(
                        err_body.get("error_reason", ""),
                        payment_id=err_body.get("payment_id", ""),
                        refund_eta_seconds=err_body.get("refund_eta_seconds"),
                        error_reason=err_body.get("error_reason", ""),
                        payment_status=payment_status,
                    )

                raise Exception(f"Tool call failed after payment: {retry.text}")

            result = retry.json()

            # Base settlement returns the tx hash inside the response envelope.
            if not tx_hash and isinstance(result, dict):
                tx_hash = (result.get("payment") or {}).get("tx_hash", "") or \
                          (result.get("receipt") or {}).get("tx_hash", "") or ""

            self.call_log.append({
                "tool": tool_name,
                "amount_usdc": amount_usdc,
                "tx_hash": tx_hash,
                "success": True,
            })

            logger.info(f"  ✓ Result received for {tool_name}")
            return result
