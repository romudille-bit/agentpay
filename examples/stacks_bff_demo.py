"""Stacks demo in three beats (BFF ARMY bootcamp, Oct 2026; comments in Spanish, as presented).
Free call, a cap refusing a paid call before anything is signed, then one real $0.01 sBTC payment.
Needs `pip install "agentpay-x402[stacks]"` and STACKS_AGENT_KEY (a funded Stacks mainnet key).
Run one beat with --free | --refuse | --pay, or everything with no flag.
"""
import os
import sys

from agentpay import quickstart, BudgetExceeded

key = os.environ["STACKS_AGENT_KEY"]   # clave privada de una wallet de Stacks mainnet con unos sats
PARAMS = {"symbol": "STX", "side": "long", "size_usd": 1000, "venue": "bitflow"}


def gratis():
    # 1. una herramienta gratis — sin fondos, con recibo
    s = quickstart(stacks_key=key, prefer_chain="stacks", max_spend="0.005", quiet=True)
    print("wallet del agente:", s.stacks_public_key, "| tope de sesión: $" + str(s.max_spend))
    r = s.call("fear_greed_index", {})
    print("fear & greed:", r.data["value"], "| costo", r.cost, "| recibo", r.tx)
    return s


def tope(s=None):
    # 2. el tope: $0.005 no alcanza para una herramienta de $0.01 → rechazada antes de firmar nada
    s = s or quickstart(stacks_key=key, prefer_chain="stacks", max_spend="0.005", quiet=True)
    print("pre_trade_check cuesta $0.01; tope de sesión: $" + str(s.max_spend))
    try:
        s.call("pre_trade_check", PARAMS)
    except BudgetExceeded as e:
        print("rechazada antes de firmar:", e)


def pago():
    # 3. tope $0.05 → un pago real de $0.01 en sBTC
    s = quickstart(stacks_key=key, prefer_chain="stacks", max_spend="0.05", quiet=True)
    print("tope de sesión: $" + str(s.max_spend), "| pagando pre_trade_check en sBTC ...")
    r = s.call("pre_trade_check", PARAMS)
    liq = r.data["factors"]["liquidity"]
    print("veredicto:", r.data["verdict"], "| venue:", r.data["venue"], "| no medido:", ", ".join(r.data.get("not_checked", [])) or "-")
    print("Bitflow:", liq["reason"])
    if liq.get("route"):   # solo cuando Bitflow cotizó la ruta
        print("  ruta:", " → ".join(c.split(".")[-1] for c in liq["route"]))
    for pool in liq.get("pools", []):
        tvl, vol = pool.get("tvl_usd"), pool.get("volume_usd_1d")
        print(f"  pool {pool['pool_id']}: TVL " + (f"${tvl:,.0f}" if tvl is not None else "n/d")
              + " | volumen 24h " + (f"${vol:,.0f}" if vol is not None else "n/d"))
    print("tx: https://explorer.hiro.so/txid/0x" + r.tx + "?chain=mainnet")
    print(s.spending_summary())


paso = sys.argv[1] if len(sys.argv) > 1 else ""
if paso == "--free":
    gratis()
elif paso == "--refuse":
    tope()
elif paso == "--pay":
    pago()
else:
    tope(gratis())
    pago()
