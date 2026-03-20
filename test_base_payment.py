"""
test_base_payment.py — Real EIP-3009 x402 payment on Base mainnet to AgentPay.

Settlement mode: Direct on-chain (no CDP facilitator dependency).

Flow:
  1. POST /tools/token_price/call  → 402 with Base payment requirements
  2. Sign EIP-712 TransferWithAuthorization off-chain (web3.py, gas-free)
  3. Call USDC.transferWithAuthorization() on-chain with the signature
     → tx submitted and confirmed on Base mainnet
  4. POST /tools/token_price/call again with PAYMENT-SIGNATURE header
     containing {"tx_hash": "0x...", "payer": "0x..."}
  5. Gateway verifies tx receipt via JSON-RPC → returns tool result

USDC contract (Base mainnet): 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
EIP-712 domain: name="USDC", version="2", chainId=8453

Setup:
  export BASE_PRIVATE_KEY=0x<your-key>   # wallet with Base mainnet USDC + ETH
  python test_base_payment.py
"""

import base64
import json
import os
import secrets
import sys
import time

import httpx
from eth_account import Account
from web3 import Web3

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_URL   = os.getenv("AGENTPAY_GATEWAY_URL", "https://gateway-production-2cc2.up.railway.app")
PRIVATE_KEY   = os.getenv("BASE_PRIVATE_KEY", "")
BASE_RPC_URL  = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")

TOOL_NAME     = "token_price"
TOOL_PARAMS   = {"symbol": "ETH"}

# Base mainnet USDC (Circle, EIP-3009 supported, 6 decimals)
CHAIN_ID      = 8453
USDC_ADDRESS  = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6

# EIP-712 domain (Base mainnet USDC: name="USD Coin", version="2", chainId=8453)
EIP712_DOMAIN = {
    "name":              "USD Coin",
    "version":           "2",
    "chainId":           CHAIN_ID,
    "verifyingContract": USDC_ADDRESS,
}

TRANSFER_AUTH_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from",        "type": "address"},
        {"name": "to",          "type": "address"},
        {"name": "value",       "type": "uint256"},
        {"name": "validAfter",  "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce",       "type": "bytes32"},
    ]
}

# transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)
USDC_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "from",        "type": "address"},
            {"name": "to",          "type": "address"},
            {"name": "value",       "type": "uint256"},
            {"name": "validAfter",  "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce",       "type": "bytes32"},
            {"name": "v",           "type": "uint8"},
            {"name": "r",           "type": "bytes32"},
            {"name": "s",           "type": "bytes32"},
        ],
        "name": "transferWithAuthorization",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def die(msg: str) -> None:
    print(f"\n✗ {msg}", file=sys.stderr)
    sys.exit(1)


def atomic_to_usdc(atomic: int) -> float:
    return atomic / 10 ** USDC_DECIMALS


def encode_payment_signature(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def sign_transfer_authorization(
    private_key: str,
    from_addr: str,
    to_addr: str,
    value_atomic: int,
    valid_before: int,
) -> tuple[str, bytes, int, bytes, bytes]:
    """
    Sign EIP-712 TransferWithAuthorization.

    Returns (full_signature_hex, nonce_bytes, v, r_bytes, s_bytes).
    The v/r/s are needed to call transferWithAuthorization on-chain.
    """
    nonce_bytes = secrets.token_bytes(32)

    signed = Account.sign_typed_data(
        private_key   = private_key,
        domain_data   = EIP712_DOMAIN,
        message_types = TRANSFER_AUTH_TYPES,
        message_data  = {
            "from":        Web3.to_checksum_address(from_addr),
            "to":          Web3.to_checksum_address(to_addr),
            "value":       value_atomic,
            "validAfter":  0,
            "validBefore": valid_before,
            "nonce":       nonce_bytes,
        },
    )
    sig = signed.signature  # HexBytes, 65 bytes: r(32) + s(32) + v(1)
    r = sig[:32]
    s = sig[32:64]
    v = sig[64]  # 27 or 28 for EIP-712; eth_account may use 0/1 → normalize
    if v < 27:
        v += 27

    return "0x" + sig.hex(), nonce_bytes, v, r, s


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not PRIVATE_KEY:
        die(
            "BASE_PRIVATE_KEY not set.\n\n"
            "  export BASE_PRIVATE_KEY=0x<your-key>\n\n"
            "  Get Base Sepolia USDC : https://faucet.circle.com  (select 'Base Sepolia')\n"
            "  Get Base Sepolia ETH  : https://faucet.quicknode.com/base/sepolia"
        )

    account = Account.from_key(PRIVATE_KEY)
    payer   = account.address
    print(f"Payer address : {payer}")

    # ── Connect to Base Sepolia ───────────────────────────────────────────────
    w3   = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    if not w3.is_connected():
        die(f"Cannot connect to Base Sepolia RPC: {BASE_RPC_URL}")

    usdc = w3.eth.contract(
        address=w3.to_checksum_address(USDC_ADDRESS),
        abi=USDC_ABI,
    )

    balance_atomic  = usdc.functions.balanceOf(payer).call()
    balance_usdc    = atomic_to_usdc(balance_atomic)
    eth_balance_wei = w3.eth.get_balance(payer)
    eth_balance     = eth_balance_wei / 1e18
    print(f"USDC balance  : {balance_usdc:.6f} USDC")
    print(f"ETH balance   : {eth_balance:.6f} ETH  (for gas)")

    if eth_balance_wei == 0:
        die(
            "No ETH for gas.\n"
            "  Get Base Sepolia ETH: https://faucet.quicknode.com/base/sepolia"
        )

    # ── Step 1: Get 402 challenge ─────────────────────────────────────────────
    print(f"\n── Step 1: Request tool (expect 402) ──────────────────────────────")
    url = f"{GATEWAY_URL}/tools/{TOOL_NAME}/call"
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json={"params": TOOL_PARAMS})

    if resp.status_code != 402:
        die(f"Expected 402, got {resp.status_code}: {resp.text[:300]}")

    challenge = resp.json()
    base_opt  = challenge.get("payment_options", {}).get("base")
    if not base_opt:
        die("402 response missing 'payment_options.base' — is BASE_GATEWAY_ADDRESS set on Railway?")

    pay_to      = base_opt["pay_to"]
    amount_usdc = float(base_opt["amount_usdc"])
    amount_atom = int(base_opt["amount_atomic"])

    print(f"  pay_to   : {pay_to}")
    print(f"  amount   : {amount_usdc} USDC ({amount_atom} atomic units)")
    print(f"  network  : {base_opt['network']}")

    if balance_atomic < amount_atom:
        die(
            f"Insufficient USDC: have {balance_usdc:.6f}, need {amount_usdc}.\n"
            "  Get testnet USDC: https://faucet.circle.com (select 'Base Sepolia')"
        )

    # ── Step 2: Sign EIP-712 TransferWithAuthorization (off-chain, no gas) ───
    print(f"\n── Step 2: Sign EIP-712 TransferWithAuthorization (off-chain) ──────")
    valid_before = int(time.time()) + 300  # 5-minute window

    t0 = time.perf_counter()
    sig_hex, nonce_bytes, v, r, s = sign_transfer_authorization(
        private_key  = PRIVATE_KEY,
        from_addr    = payer,
        to_addr      = pay_to,
        value_atomic = amount_atom,
        valid_before = valid_before,
    )
    sign_ms = (time.perf_counter() - t0) * 1000

    nonce_hex = "0x" + nonce_bytes.hex()
    print(f"  signature  : {sig_hex[:22]}...{sig_hex[-8:]}  ({len(sig_hex)} chars)")
    print(f"  v / r / s  : {v} / {r.hex()[:12]}... / {s.hex()[:12]}...")
    print(f"  nonce      : {nonce_hex[:20]}...")
    print(f"  validBefore: {valid_before} ({time.strftime('%H:%M:%S UTC', time.gmtime(valid_before))})")
    print(f"  sign time  : {sign_ms:.1f}ms  (EIP-712, no gas)")

    # ── Step 3: Submit transferWithAuthorization on-chain ────────────────────
    print(f"\n── Step 3: Submit transferWithAuthorization on-chain ───────────────")
    gas_price = w3.eth.gas_price
    print(f"  gas price  : {gas_price / 1e9:.4f} gwei")

    tx = usdc.functions.transferWithAuthorization(
        Web3.to_checksum_address(payer),
        Web3.to_checksum_address(pay_to),
        amount_atom,
        0,            # validAfter
        valid_before,
        nonce_bytes,  # bytes32
        v,
        r,            # bytes32
        s,            # bytes32
    ).build_transaction({
        "chainId":  CHAIN_ID,
        "from":     payer,
        "nonce":    w3.eth.get_transaction_count(payer),
        "gasPrice": gas_price,
    })

    # Estimate gas
    try:
        tx["gas"] = w3.eth.estimate_gas(tx)
    except Exception as e:
        die(f"Gas estimation failed — tx would revert: {e}")

    gas_cost_eth = tx["gas"] * gas_price / 1e18
    print(f"  gas limit  : {tx['gas']}  (cost ≈ {gas_cost_eth:.8f} ETH)")

    signed_tx  = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    t1         = time.perf_counter()
    tx_hash    = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    tx_hash_hex = "0x" + tx_hash.hex()
    print(f"  tx sent    : {tx_hash_hex}")
    print(f"  explorer   : https://basescan.org/tx/{tx_hash_hex}")
    print(f"  waiting for confirmation...", end="", flush=True)

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    elapsed = time.perf_counter() - t1
    status  = "✓ confirmed" if receipt.status == 1 else "✗ reverted"
    print(f"\r  {status} in {elapsed:.1f}s  (block {receipt.blockNumber})")

    if receipt.status != 1:
        die("Transaction reverted. Check that nonce hasn't been used before (each nonce can only be used once).")

    # ── Step 4: Tell gateway about the tx ────────────────────────────────────
    print(f"\n── Step 4: Retry with PAYMENT-SIGNATURE (Mode B: on-chain tx) ──────")
    sig_payload = {
        "tx_hash": tx_hash_hex,
        "payer":   payer,
    }
    payment_sig_header = encode_payment_signature(sig_payload)
    print(f"  payload    : {json.dumps(sig_payload)}")
    print(f"  header len : {len(payment_sig_header)} chars")
    print(f"  sending to gateway for receipt verification...")

    t2 = time.perf_counter()
    with httpx.Client(timeout=45.0) as client:
        resp2 = client.post(
            url,
            json    = {"params": TOOL_PARAMS},
            headers = {"payment-signature": payment_sig_header},
        )
    verify_ms = (time.perf_counter() - t2) * 1000

    print(f"  status     : {resp2.status_code}  ({verify_ms:.0f}ms)")

    if resp2.status_code != 200:
        print(f"\n✗ Gateway rejected payment:")
        try:
            print(json.dumps(resp2.json(), indent=2))
        except Exception:
            print(resp2.text[:500])
        sys.exit(1)

    # ── Step 5: Display result ────────────────────────────────────────────────
    data = resp2.json()
    print(f"\n── Result ──────────────────────────────────────────────────────────")

    result = data.get("result") or data
    if isinstance(result, dict):
        for k, v_ in result.items():
            if k not in ("payment_receipt", "_payment", "payment_id"):
                print(f"  {k:<18}: {v_}")
    else:
        print(f"  result            : {result}")

    print(f"\n✓  Base Sepolia x402 payment complete")
    print(f"   Paid   : {amount_usdc} USDC via EIP-3009 transferWithAuthorization")
    print(f"   Tx     : {tx_hash_hex}")
    print(f"   Tool   : {TOOL_NAME}({TOOL_PARAMS})")


if __name__ == "__main__":
    main()
