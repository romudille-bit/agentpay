"""Fifteen-line Stacks demo (BFF ARMY bootcamp, Oct 2026; comments in Spanish, as presented).
Free call, a cap refusing a paid call before anything is signed, then one real $0.01 sBTC payment.
Needs `pip install "agentpay-x402[stacks]"` and STACKS_AGENT_KEY (a funded Stacks mainnet key).
"""
import os
from agentpay import quickstart, BudgetExceeded

key = os.environ["STACKS_AGENT_KEY"]   # clave privada de una wallet de Stacks mainnet con unos sats

# 1. una herramienta gratis — sin fondos, con recibo
s = quickstart(stacks_key=key, prefer_chain="stacks", max_spend="0.005", quiet=True)
print("wallet del agente:", s.stacks_public_key)
r = s.call("fear_greed_index", {})
print("fear & greed:", r.data["value"], "| costo", r.cost, "| recibo", r.tx)

# 2. el tope: $0.005 no alcanza para una herramienta de $0.01 → rechazada antes de firmar nada
try:
    s.call("pre_trade_check", {"symbol": "STX", "size_usd": 1000, "venue": "bitflow"})
except BudgetExceeded as e:
    print("rechazada antes de firmar:", e)

# 3. tope $0.05 → un pago real de $0.01 en sBTC
s = quickstart(stacks_key=key, prefer_chain="stacks", max_spend="0.05", quiet=True)
r = s.call("pre_trade_check", {"symbol": "STX", "side": "long", "size_usd": 1000, "venue": "bitflow"})
print("veredicto:", r.data["verdict"], "|", r.data["factors"]["liquidity"]["reason"])
print("tx: https://explorer.hiro.so/txid/0x" + r.tx + "?chain=mainnet")
print(s.spending_summary())
