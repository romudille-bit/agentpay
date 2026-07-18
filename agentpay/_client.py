"""
_client.py — HTTP client that handles the x402 payment flow.

Internal helper used by Session.call(). Not part of the public API.

Failure semantics (AGE-53..56):
  - PrePaymentError   → nothing moved, no signed auth left the process.
                        Session.call() may safely fall back to another tool.
  - PaymentFailed     → the on-chain payment itself failed (no funds moved).
  - BudgetExceeded    → the 402 demanded more than the caller's cap; refused
                        BEFORE paying or signing.
  - RefundPending     → paid, tool failed, gateway queued a refund. The spend
                        is recorded in call_log and counts against the budget
                        until the refund confirms.
  - Exception ("Tool call failed after payment…") → funds moved (or a signed
                        authorization was transmitted) and the call then
                        failed. The spend is recorded in call_log. Callers
                        MUST NOT retry with a second payment.

call_log entries are appended the moment value can leave the wallet — at
Stellar broadcast or Base auth transmission — not when the call returns 200,
so Session budgets count every real payment even when the tool then fails.
"""

import httpx
import logging
from decimal import Decimal

from agentpay._wallet import (
    AgentWallet,
    BudgetExceeded,
    PaymentFailed,
    PrePaymentError,
    RefundPending,
)

logger = logging.getLogger(__name__)


class AgentPayClient:
    """
    HTTP client that handles the full x402 payment flow.

    When a tool returns 402:
      1. Reads payment details from response
      2. Sends USDC via Stellar (or signs a Base EIP-3009 authorization)
      3. Retries with payment proof header
    """

    def __init__(self, wallet: AgentWallet, gateway_url: str):
        self.wallet = wallet
        self.gateway_url = gateway_url
        self.call_log: list[dict] = []

    def _sign_base_auth(self, base_opt: dict, url: str) -> str:
        """
        Sign the gasless EIP-3009 (Mode A) authorization for a paid AgentPay
        tool and return the header payload. OFF-CHAIN only — nothing is
        transmitted here, so a failure in this step is strictly pre-payment
        and the caller may still fall back to Stellar (AGE-56).

        `base_opt` is the `payment_options.base` block from AgentPay's native
        402.
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
            f"  Signing Base auth (EIP-3009, gasless) "
            f"{base_opt.get('amount_usdc')} USDC → {str(accept['payTo'])[:10]}..."
        )
        return self.wallet.build_base_payment_signature(accept, url)

    @staticmethod
    def _base_opt_amount_usd(base_opt: dict) -> Decimal | None:
        """USD amount the Base option would sign for, or None if unparseable."""
        try:
            atomic = base_opt.get("amount_atomic") or base_opt.get("amount")
            if atomic is not None:
                return Decimal(str(atomic)) / Decimal("1000000")
            usd = base_opt.get("amount_usdc")
            return Decimal(str(usd)) if usd is not None else None
        except (ValueError, ArithmeticError):
            return None

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

        Raises BudgetExceeded (BEFORE paying or signing) if max_spend is set
        and the amount the 402 actually demands exceeds it — this is the hard
        cap AGE-53 requires: the 402 body's amount, not the registry quote,
        is what gets checked.

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
            try:
                resp = client.post(url, json=payload)
            except Exception as e:
                raise PrePaymentError(
                    f"request to '{tool_name}' failed before payment: {e}"
                )

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
                try:
                    return resp.json()
                except Exception as e:
                    # $0 and nothing transmitted — safe for the caller to
                    # fall back.
                    raise PrePaymentError(
                        f"free tool '{tool_name}' returned 200 with an "
                        f"unparseable body: {e}"
                    )

            if resp.status_code != 402:
                raise PrePaymentError(f"Unexpected {resp.status_code}: {resp.text}")

            # ── 402 received — parse payment details ───────────────────────
            try:
                data = resp.json()
                payment_id  = data["payment_id"]
                amount_usdc = data["amount_usdc"]
                pay_to      = data["pay_to"]
            except Exception as e:
                raise PrePaymentError(
                    f"could not parse 402 response for '{tool_name}': {e}"
                )

            logger.info(f"  402 — pay {amount_usdc} USDC to {pay_to[:12]}...")

            # ── Budget cap vs the amount ACTUALLY demanded (AGE-53) ─────────
            # Session.call() passes max_spend = min(remaining budget,
            # quoted_price * (1 + tolerance)). Checked BEFORE any signing or
            # broadcast, so a gateway that advertises $0.001 in the registry
            # and demands more in the 402 is refused, not paid.
            if max_spend is not None and Decimal(str(amount_usdc)) > Decimal(str(max_spend)):
                raise BudgetExceeded(
                    f"402 for '{tool_name}' demands {amount_usdc} USDC, which exceeds "
                    f"the cap for this call ({max_spend} USDC) — refusing to pay"
                )

            # `entry` is the call_log record for this payment. It is appended
            # the moment value can leave the wallet (AGE-54) and flipped to
            # success=True only when the tool call completes.
            entry: dict | None = None

            def _record(state: str, tx_hash: str = "") -> dict:
                nonlocal entry
                entry = {
                    "tool": tool_name,
                    "amount_usdc": str(amount_usdc),
                    "tx_hash": tx_hash,
                    "success": False,
                    "state": state,
                }
                self.call_log.append(entry)
                return entry

            tx_hash = ""

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
                _record("free", tx_hash)
                proof_header = (
                    f"tx_hash={tx_hash},"
                    f"from={self.wallet.public_key},"
                    f"id={payment_id}"
                )
                try:
                    retry = client.post(
                        url,
                        json=payload,
                        headers={
                            "X-Payment": proof_header,
                            "X-Agent-Address": self.wallet.public_key,
                        },
                    )
                except Exception as e:
                    # $0 — nothing at risk, safe for the caller to fall back.
                    raise PrePaymentError(
                        f"free tool '{tool_name}' retry failed: {e}"
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
                    # ── Phase 1: sign OFF-CHAIN (strictly pre-payment) ─────
                    sig = None
                    try:
                        # AGE-53: the Base option can carry its own amount —
                        # bound it by the same cap before signing anything.
                        # Fail closed: an amount we can't parse is an amount we
                        # can't bound, so we don't sign it either (falls back
                        # to Stellar via the except below — Stellar pays the
                        # body's amount_usdc, which IS capped).
                        base_amount = self._base_opt_amount_usd(base_opt)
                        if max_spend is not None and base_amount is None:
                            raise ValueError(
                                f"Base option for '{tool_name}' has an "
                                f"unparseable amount — refusing to sign"
                            )
                        if (
                            max_spend is not None
                            and base_amount > Decimal(str(max_spend))
                        ):
                            raise BudgetExceeded(
                                f"Base option for '{tool_name}' would sign for "
                                f"{base_amount} USDC, which exceeds the cap for "
                                f"this call ({max_spend} USDC) — refusing to sign"
                            )
                        sig = self._sign_base_auth(base_opt, url)
                    except BudgetExceeded:
                        raise
                    except Exception as e:
                        msg = str(e)[:160]
                        if isinstance(e, ImportError):
                            msg += ' — install the Base extra: pip install "agentpay-x402[base]"'
                        if chain_is_explicit and prefer_chain == "base":
                            raise PaymentFailed(f"base settlement failed: {msg}")
                        logger.warning(
                            f"  Base signing failed ({msg}) — falling back to Stellar"
                        )
                        sig = None

                    if sig is not None:
                        # ── Phase 2: transmit the signed auth (AGE-56) ─────
                        # Once this POST leaves the wire the gateway holds a
                        # transferWithAuthorization it can settle within
                        # validBefore — even if it answers non-200. Record the
                        # spend NOW and never fall back to Stellar past this
                        # point: that would be a second payment.
                        #
                        # PAYMENT-SIGNATURE only. Sending the same payload in
                        # X-PAYMENT (the x402 standard header) collides with
                        # the gateway's legacy Stellar X-Payment header and got
                        # every Mode A named-tool call rejected with 'Invalid
                        # X-Payment header format'. This path only talks to
                        # AgentPay's own gateway; external x402 URLs go through
                        # _call_x402_url instead.
                        _record("signed_auth_transmitted")
                        try:
                            retry = client.post(
                                url,
                                json=payload,
                                headers={
                                    "PAYMENT-SIGNATURE": sig,
                                    "X-Agent-Address":   self.wallet.base_address,
                                },
                            )
                        except Exception as e:
                            entry["state"] = "uncertain_settlement"
                            raise Exception(
                                f"Tool call failed after payment — the signed Base "
                                f"authorization was transmitted, settlement uncertain, "
                                f"spend recorded: {e}"
                            )

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
                    # Funds have LEFT the wallet — record before the retry, so
                    # a failed retry still burns budget (AGE-54).
                    _record("paid_awaiting_result", tx_hash)
                    proof_header = (
                        f"tx_hash={tx_hash},"
                        f"from={self.wallet.public_key},"
                        f"id={payment_id}"
                    )
                    try:
                        retry = client.post(
                            url,
                            json=payload,
                            headers={
                                "X-Payment": proof_header,
                                "X-Agent-Address": self.wallet.public_key,
                            },
                        )
                    except Exception as e:
                        entry["state"] = "paid_no_result"
                        raise Exception(
                            f"Tool call failed after payment (spend recorded): {e}"
                        )
                else:
                    tx_hash = retry.headers.get("x-tx-hash", "") or ""
                    if entry is not None and tx_hash:
                        entry["tx_hash"] = tx_hash

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
                    if entry is not None:
                        # Spend stays counted against the budget until the
                        # refund actually confirms (AGE-54).
                        entry["state"] = "refund_pending"
                    raise RefundPending(
                        err_body.get("error_reason", ""),
                        payment_id=err_body.get("payment_id", ""),
                        refund_eta_seconds=err_body.get("refund_eta_seconds"),
                        error_reason=err_body.get("error_reason", ""),
                        payment_status=payment_status,
                    )

                if entry is None or Decimal(str(entry["amount_usdc"] or "0")) == 0:
                    # Nothing at risk ($0 free proof) — safe to fall back.
                    raise PrePaymentError(
                        f"free tool '{tool_name}' call failed: "
                        f"{retry.status_code} {retry.text[:200]}"
                    )

                entry["state"] = (
                    "uncertain_settlement"
                    if entry["state"] == "signed_auth_transmitted"
                    else "paid_no_result"
                )
                raise Exception(f"Tool call failed after payment: {retry.text}")

            try:
                result = retry.json()
            except Exception as e:
                if entry is None or Decimal(str(entry["amount_usdc"] or "0")) == 0:
                    # $0 — nothing at risk, safe for the caller to fall back.
                    raise PrePaymentError(
                        f"free tool '{tool_name}' returned 200 with an "
                        f"unparseable body: {e}"
                    )
                # Paid: the spend is already recorded; the tool ran but the
                # body is unusable. Keep success=False so receipts don't show
                # a settled leg for a call the caller experienced as an error.
                entry["state"] = "settled_bad_body"
                raise Exception(
                    f"Tool call failed after payment: unparseable 200 body ({e})"
                )

            # Base settlement returns the tx hash inside the response envelope.
            if not tx_hash and isinstance(result, dict):
                tx_hash = (result.get("payment") or {}).get("tx_hash", "") or \
                          (result.get("receipt") or {}).get("tx_hash", "") or ""

            if entry is not None:
                entry["success"] = True
                entry["tx_hash"] = tx_hash
                if entry["state"] in ("paid_awaiting_result", "signed_auth_transmitted"):
                    entry["state"] = "settled"
                # Settlement network from the gateway receipt, if present.
                if isinstance(result, dict):
                    net = (result.get("payment") or {}).get("network", "") or ""
                    if net:
                        entry["network"] = net

            logger.info(f"  ✓ Result received for {tool_name}")
            return result
