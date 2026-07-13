"""Generate the cross-check vector for eip3009.test.mjs from the Python SDK's
own signing stack (x402[evm] + eth_account) — the reference implementation the
gateway's CDP facilitator already accepts in production.

Run: venv/bin/python npm/test/gen_vector.py
"""

from eth_account import Account
from eth_account.messages import encode_typed_data

TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

full_message = {
    "types": {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ],
    },
    "primaryType": "TransferWithAuthorization",
    "domain": {
        "name": "USD Coin",
        "version": "2",
        "chainId": 8453,
        "verifyingContract": BASE_USDC,
    },
    "message": {
        "from": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        "to": "0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7",
        "value": 10000,
        "validAfter": 1700000000,
        "validBefore": 1700000300,
        "nonce": bytes.fromhex("11" * 32),
    },
}

signable = encode_typed_data(full_message=full_message)
acct = Account.from_key(TEST_KEY)
signed = acct.sign_message(signable)

print("digest:   ", "0x" + signed.message_hash.hex().removeprefix("0x"))
print("signature:", "0x" + signed.signature.hex().removeprefix("0x"))
