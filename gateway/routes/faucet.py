"""
routes/faucet.py — Testnet wallet provisioning faucet.

  GET /faucet      — JSON, generates a funded testnet wallet
  GET /faucet/ui   — browser-friendly HTML page that calls /faucet via fetch

Mainnet returns 404 for both endpoints (no faucet on real money). Testnet
enforces a 10-minute IP cooldown and a 3-second anti-script delay before
provisioning.
"""

import asyncio
import logging
import textwrap
import time as _time
from decimal import Decimal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from gateway._limiter import limiter
from gateway.config import GATEWAY_URL, settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Maps IP → epoch timestamp of last successful faucet request.
# Requests within the cooldown window of a prior grant are rejected.
# Testnet USDC has no dollar cost — the limit exists only to stop script farms.
# Normal onboarding (dev iterates, demo, tries again) should not be blocked.
_FAUCET_IP_LOG: dict[str, float] = {}
_FAUCET_COOLDOWN_SECS = 600  # 10 minutes — lets devs iterate, still stops farms


async def _provision_wallet(base_url: str) -> dict:
    """
    Create and fund a fresh Stellar testnet wallet with XLM + 0.05 USDC.

    Steps:
      1. Generate keypair
      2. Fund with XLM via Friendbot
      3. Add USDC trustline (signed by new keypair)
      4. Send 0.05 USDC from gateway wallet (checks balance ≥ 1 USDC first)
      5. Return balances + ready-to-use code snippet
    """
    from stellar_sdk import Keypair, TransactionBuilder
    from gateway.stellar import get_server, get_network_passphrase, get_usdc_asset

    server             = get_server()
    network_passphrase = get_network_passphrase()
    usdc               = get_usdc_asset()

    if not settings.GATEWAY_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Gateway wallet not configured")

    # ── 1. Generate keypair ───────────────────────────────────────────────────
    keypair    = Keypair.random()
    public_key = keypair.public_key
    secret_key = keypair.secret
    logger.info(f"[FAUCET] step=1/5 generated keypair {public_key[:8]}...")

    # ── 2. Fund with XLM via Friendbot ───────────────────────────────────────
    logger.info(f"[FAUCET] step=2/5 calling Friendbot")
    async with httpx.AsyncClient(timeout=60.0) as client:
        fb = await client.get(
            "https://friendbot.stellar.org/",
            params={"addr": public_key},
        )
    if fb.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Friendbot failed: {fb.text[:200]}",
        )
    logger.info(f"[FAUCET] step=2/5 Friendbot OK")

    # ── 3. Add USDC trustline (signed by new wallet) ──────────────────────────
    # asyncio.to_thread offloads the synchronous stellar_sdk calls to a worker
    # thread so the FastAPI event loop stays free for other requests during the
    # ~5-10s of Horizon round-trips a faucet provision involves.
    logger.info(f"[FAUCET] step=3/5 adding USDC trustline")
    new_account = await asyncio.to_thread(server.load_account, public_key)
    trust_tx = (
        TransactionBuilder(
            source_account=new_account,
            network_passphrase=network_passphrase,
            base_fee=100,
        )
        .append_change_trust_op(asset=usdc)
        .set_timeout(30)
        .build()
    )
    trust_tx.sign(keypair)
    await asyncio.to_thread(server.submit_transaction, trust_tx)
    logger.info(f"[FAUCET] step=3/5 trustline submitted")

    # ── 4. Send 0.05 USDC from gateway (with balance guard) ──────────────────
    logger.info(f"[FAUCET] step=4/5 checking gateway balance and sending 0.05 USDC")
    gateway_keypair = Keypair.from_secret(settings.GATEWAY_SECRET_KEY)
    from gateway.stellar import get_usdc_balance
    gateway_usdc = Decimal(await get_usdc_balance(gateway_keypair.public_key))
    if gateway_usdc < Decimal("1"):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Faucet is temporarily empty (balance: {gateway_usdc} USDC). "
                "Please try again later or reach out on GitHub."
            ),
        )
    gateway_account = await asyncio.to_thread(
        server.load_account, gateway_keypair.public_key
    )
    pay_tx = (
        TransactionBuilder(
            source_account=gateway_account,
            network_passphrase=network_passphrase,
            base_fee=100,
        )
        .append_payment_op(
            destination=public_key,
            asset=usdc,
            amount="0.05",
        )
        .set_timeout(30)
        .build()
    )
    pay_tx.sign(gateway_keypair)
    await asyncio.to_thread(server.submit_transaction, pay_tx)
    logger.info(f"[FAUCET] step=4/5 USDC sent")

    # ── 5. Read balances ──────────────────────────────────────────────────────
    logger.info(f"[FAUCET] step=5/5 reading final balances")
    funded = await asyncio.to_thread(server.load_account, public_key)
    xlm_balance  = "0"
    usdc_balance = "0"
    for b in funded.raw_data.get("balances", []):
        if b.get("asset_type") == "native":
            xlm_balance = b["balance"]
        elif b.get("asset_code") == "USDC":
            usdc_balance = b["balance"]

    # ── 6. Python code snippet ────────────────────────────────────────────────
    gateway_url = base_url
    snippet = textwrap.dedent(f"""\
        from agent.wallet import AgentWallet, Session

        wallet = AgentWallet(
            secret_key="{secret_key}",
            network="testnet",
        )

        GATEWAY = "{gateway_url}"

        with Session(wallet=wallet, gateway_url=GATEWAY, max_spend="0.05") as session:
            r = session.call("token_price", {{"symbol": "ETH"}})
            print(f"ETH: ${{r['result']['price_usd']:,.2f}}")

            r = session.call("gas_tracker", {{}})
            print(f"Gas: {{r['result']['fast_gwei']}} gwei")

            print(f"Spent: {{session.spent()}}  Remaining: {{session.remaining()}}")
    """)

    logger.info(f"[FAUCET] done — wallet {public_key[:8]}... usdc={usdc_balance} xlm={xlm_balance}")
    return {
        "public_key":   public_key,
        "secret_key":   secret_key,
        "usdc_balance": usdc_balance,
        "xlm_balance":  xlm_balance,
        "network":      "testnet",
        "gateway_url":  gateway_url,
        "snippet":      snippet,
        "warning":      "⚠️ Testnet only. Never share your secret key on mainnet. This wallet is for testing AgentPay only.",
    }


@router.get("/faucet")
@limiter.limit("30/hour")
async def faucet_json(request: Request):
    """Generate a funded testnet wallet — returns JSON."""
    if settings.STELLAR_NETWORK == "mainnet":
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Faucet not available on mainnet.",
                "message": "Fund your Stellar mainnet wallet with USDC to use AgentPay.",
                "docs": "https://github.com/romudille-bit/agentpay",
            },
        )
    # ── IP cooldown: one wallet per IP per 24 hours ───────────────────────────
    client_ip = request.client.host if request.client else "unknown"
    now = _time.time()
    last = _FAUCET_IP_LOG.get(client_ip, 0)
    if now - last < _FAUCET_COOLDOWN_SECS:
        wait_s = int(_FAUCET_COOLDOWN_SECS - (now - last))
        wait_label = f"{wait_s}s" if wait_s < 120 else f"~{wait_s // 60}min"
        raise HTTPException(
            status_code=429,
            detail=(
                f"This IP just received a test wallet. Try again in {wait_label}. "
                f"Or switch to mainnet — each tool call costs ~$0.001–$0.005 USDC."
            ),
        )

    # ── Anti-script delay (3 seconds) ─────────────────────────────────────────
    await asyncio.sleep(3)

    base_url = settings.AGENTPAY_GATEWAY_URL or GATEWAY_URL
    result = await _provision_wallet(base_url)

    # Record IP only after successful wallet creation
    _FAUCET_IP_LOG[client_ip] = _time.time()
    return result


@router.get("/faucet/ui", response_class=HTMLResponse)
async def faucet_ui():
    """Browser-friendly faucet page."""
    if settings.STELLAR_NETWORK == "mainnet":
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Faucet not available on mainnet.",
                "message": "Fund your Stellar mainnet wallet with USDC to use AgentPay.",
                "docs": "https://github.com/romudille-bit/agentpay",
            },
        )
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AgentPay Faucet — Get a Test Wallet</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
      background: #0d0d0d; color: #e8e8e8; min-height: 100vh;
      display: flex; flex-direction: column; align-items: center;
      padding: 3rem 1rem;
    }
    h1 { font-size: 2rem; font-weight: 700; margin-bottom: .4rem; }
    .subtitle { color: #888; margin-bottom: 2.5rem; font-size: 1rem; }
    .card {
      background: #181818; border: 1px solid #2a2a2a; border-radius: 12px;
      padding: 2rem; max-width: 640px; width: 100%;
    }
    button {
      width: 100%; padding: 1rem; font-size: 1.1rem; font-weight: 600;
      background: #7c3aed; color: #fff; border: none; border-radius: 8px;
      cursor: pointer; transition: background .2s;
    }
    button:hover:not(:disabled) { background: #6d28d9; }
    button:disabled { background: #3a3a3a; cursor: not-allowed; }
    .spinner {
      display: none; text-align: center; color: #888;
      margin-top: 1.5rem; font-size: .9rem;
    }
    .result { display: none; margin-top: 1.8rem; }
    .field { margin-bottom: 1.2rem; }
    .label { font-size: .75rem; text-transform: uppercase; letter-spacing: .08em;
             color: #888; margin-bottom: .35rem; }
    .value {
      font-family: "SF Mono", "Fira Code", monospace; font-size: .85rem;
      background: #111; border: 1px solid #2a2a2a; border-radius: 6px;
      padding: .6rem .8rem; word-break: break-all; position: relative;
    }
    .balances { display: flex; gap: 1rem; }
    .balance-box {
      flex: 1; background: #111; border: 1px solid #2a2a2a; border-radius: 8px;
      padding: 1rem; text-align: center;
    }
    .balance-amount { font-size: 1.5rem; font-weight: 700; color: #a78bfa; }
    .balance-token  { font-size: .8rem; color: #888; margin-top: .2rem; }
    .snippet-wrap {
      background: #111; border: 1px solid #2a2a2a; border-radius: 6px;
      padding: 1rem; overflow-x: auto;
    }
    pre { font-size: .8rem; line-height: 1.6; color: #c4b5fd; }
    .copy-btn {
      width: auto; padding: .35rem .8rem; font-size: .8rem;
      background: #2a2a2a; border-radius: 4px; margin-top: .5rem;
    }
    .copy-btn:hover { background: #3a3a3a; }
    .warning {
      margin-top: 1.5rem; padding: .75rem 1rem;
      background: #1c1200; border: 1px solid #4a3000; border-radius: 6px;
      font-size: .82rem; color: #f59e0b;
    }
    .error {
      margin-top: 1.5rem; padding: .75rem 1rem;
      background: #1c0000; border: 1px solid #4a0000; border-radius: 6px;
      color: #f87171;
    }
  </style>
</head>
<body>
  <h1>AgentPay Faucet</h1>
  <p class="subtitle">Get a funded Stellar testnet wallet — ready to call paid tools in seconds.</p>

  <div class="card">
    <button id="btn" onclick="getWallet()">Get Test Wallet</button>
    <div class="spinner" id="spinner">
      ⏳ Creating wallet, adding trustline, sending USDC… (~5–10s)
    </div>

    <div class="result" id="result">
      <div class="balances" id="balances"></div>

      <div class="field" style="margin-top:1.2rem">
        <div class="label">Public Key</div>
        <div class="value" id="pub"></div>
      </div>

      <div class="field">
        <div class="label">Secret Key — keep this private!</div>
        <div class="value" id="sec" style="color:#f87171"></div>
      </div>

      <div class="field">
        <div class="label">Ready-to-use Python snippet</div>
        <div class="snippet-wrap"><pre id="snip"></pre></div>
        <button class="copy-btn" onclick="copySnippet()">Copy snippet</button>
      </div>

      <div class="warning">
        ⚠️ Testnet only. Never share your secret key on mainnet. This wallet is for testing AgentPay only.
      </div>
    </div>

    <div class="error" id="error" style="display:none"></div>
  </div>

  <script>
    async function getWallet() {
      const btn     = document.getElementById('btn');
      const spinner = document.getElementById('spinner');
      const result  = document.getElementById('result');
      const errBox  = document.getElementById('error');

      btn.disabled      = true;
      spinner.style.display = 'block';
      result.style.display  = 'none';
      errBox.style.display  = 'none';

      try {
        const res  = await fetch('/faucet');
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

        document.getElementById('pub').textContent  = data.public_key;
        document.getElementById('sec').textContent  = data.secret_key;
        document.getElementById('snip').textContent = data.snippet;

        document.getElementById('balances').innerHTML = `
          <div class="balance-box">
            <div class="balance-amount">${parseFloat(data.usdc_balance).toFixed(2)}</div>
            <div class="balance-token">USDC</div>
          </div>
          <div class="balance-box">
            <div class="balance-amount">${parseFloat(data.xlm_balance).toFixed(2)}</div>
            <div class="balance-token">XLM (gas)</div>
          </div>
        `;

        result.style.display = 'block';
      } catch (e) {
        errBox.textContent   = '❌ ' + e.message;
        errBox.style.display = 'block';
        btn.disabled = false;
      } finally {
        spinner.style.display = 'none';
      }
    }

    function copySnippet() {
      navigator.clipboard.writeText(document.getElementById('snip').textContent);
    }
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)
