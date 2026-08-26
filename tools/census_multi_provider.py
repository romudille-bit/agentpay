#!/usr/bin/env python3
"""
census_multi_provider.py — AGE-140: how many x402 payer wallets on Base pay
≥3 distinct providers, and what share of the dollars do they carry?

Why
---
The 2026-08-24 strategy note's thesis ("an agent paying BlockRun + Exa +
StableEnrich holds three ledgers and no total — we are the total") only has a
market if wallets that pay MORE THAN ONE provider exist in meaningful number and
carry meaningful dollars. The payer-sybil finding says 96.5% of x402 payers only
ever pay ONE recipient. This script measures the other 3.5%: count, dollar
share, and whether they look like buyers (deep, repeated spend per provider) or
probers (one payment to each of many providers — the trust layer testing
sellers, not customers).

Denominator (always stated in the report)
-----------------------------------------
v2, x402-scoped: USDC transfers on Base in the last N days that (1) settled
through an EIP-3009 authorization (AuthorizationUsed on the USDC contract),
(2) were submitted by one of the 128 Base relayer addresses in x402scan's open
facilitator registry, and (3) went to a "metered seller" — a recipient with ≥5
distinct payers and a mean leg ≤ $1. The first v1 run (EIP-3009 only) returned
$35M / 10.4M legs / 65k wallets — ~25x the x402 market — because the CDP
relayers also submit Base Pay / consumer gasless USDC sends. Filters 2+3 are
what make the set "x402 tool payments" rather than "gasless USDC on Base".

Data source
-----------
Saved Dune queries (SQL in tools/sql/) — use the *_x402.sql (v2) versions:
  * census_payers_x402.sql  → one row per payer  (legs, usd, recipients, top relayer)
  * census_pairs_x402.sql   → one row per (payer, recipient) for multi-recipient payers
  * census_sellers_x402.sql → one row per metered seller (optional; ranks our gateway)
  The v1 files (census_payers.sql / census_pairs.sql) are the UNSCOPED EIP-3009 set
  — kept for the sensitivity comparison; see the header of the v2 files for why.
Create each once in the Dune UI (paste the SQL; no parameters — the window is
hardcoded), note the query ids, then run:

    source venv/bin/activate
    python3 tools/census_multi_provider.py --payers-query-id 123456 --pairs-query-id 123457 \
        --sellers-query-id 123458 --use-latest --no-params --json

DUNE_API_KEY is read from ../.env (already present for the dune_query tool).
Or export the two result sets as CSV from the Dune UI and pass
--payers-csv / --pairs-csv instead (no API credits used).

Provider resolution (optional): if SUPABASE_URL/SUPABASE_KEY are in .env the
script pulls the distinct pay_to set from service_probes and reports what share
of the ≥3-recipient buyers' recipients (by wallet and by dollar) we can already
name. That number is the baseline for AGE-138's weekly map-coverage metric.

Output
------
reviews/CENSUS_MULTI_PROVIDER_<date>.md (untracked dir) + a headline on stdout.
Addresses are truncated in the report by default (data-sharing boundary);
--full-addresses keeps them whole for internal use.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from typing import Iterable

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

DUNE_API_BASE = "https://api.dune.com/api/v1"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Our own wallets — excluded from the payer population, reported separately.
SELF_PAYERS = {
    "0xe1601c10b8d4dbf71e0c592b779520380174bc3a",  # flagship analyst (daily cron)
    "0xc507d39678309b2389744526a7cd86e236c6c750",  # prober wallet (Mon/Thu sweeps)
}
OUR_PAYTO = "0xe8b25a72dd6aef69515452a61ad231c7df2843b7"  # AgentPay gateway on Base

# Facilitator relayer addresses on Base, from x402scan's open registry
# (Merit-Systems/x402scan, packages/external/facilitators/src/facilitators/*.ts,
# pulled 2026-08-26). 128 addresses / 30 facilitators. Re-pull when x402scan adds one.
KNOWN_RELAYERS: dict[str, str] = {
    "0x001ddabba5782ee48842318bd9ff4008647c8d9c": "Coinbase (deprecated)",
    "0x0168f80e035ea68b191faf9bfc12778c87d92008": "X402rs",
    "0x021cc47adeca6673def958e324ca38023b80a5be": "Heurist",
    "0x03a3f7ce8e21e6f8d9fa14c67d8876b2470dc2f1": "PayAI (deprecated)",
    "0x052aaae3cad5c095850246f8ffb228354c56752a": "Thirdweb (deprecated)",
    "0x06f0bfd2c8f36674df5cde852c1eed8025c268c9": "Corbits",
    "0x103040545ac5031a11e8c03dd11324c7333a13c7": "Ultravioleta DAO",
    "0x1363c7ff51ccce10258a7f7bddd63baab6aaf678": "Daydreams",
    "0x14fda13953fc30428938e6bf950d036e77214e52": "Coinbase",
    "0x15e2e2da7539ef1f652aa3c1d6142a535aa3d7ea": "Bitrefill",
    "0x16e47d275198ed65916a560bab4af6330c36ae09": "Openmid",
    "0x179761d9eed0f0d1599330cc94b0926e68ae87f1": "AnySpend",
    "0x1892f72fdb3a966b2ad8595aa5f7741ef72d6085": "RelAI",
    "0x1fc230ee3c13d0d520d49360a967dbd1555c8326": "Heurist",
    "0x222c4367a2950f3b53af260e111fc3060b0983ff": "AurraCloud",
    "0x24d4f332d8e886fc005bb4a103bad21d9ebc2b7f": "FluxA",
    "0x25659315106580ce2a787ceec5efb2d347b539c9": "PayAI (deprecated)",
    "0x279e08f711182c79ba6d09669127a426228a4653": "Daydreams",
    "0x290d8b8edcafb25042725cb9e78bcac36b8865f8": "Heurist",
    "0x2a89407a98a0732b7fd578c4e156b7166540eb5a": "Coinbase",
    "0x2bb201f1bb056eb738718bd7a3ad1bef24b883bb": "Cascade",
    "0x2daaef6f941de214bf7d6daf322bc6bc7406accb": "PayAI (deprecated)",
    "0x2fae4026a31f19183947f0a6045ef975ebfa9ca8": "PayAI (deprecated)",
    "0x3210d7b21bfe1083c9dddbe17e8f947c9029a584": "Meridian",
    "0x37dfb4033d5dd98fd335f24d0d42e8fe68d587d6": "Primer",
    "0x3a5ca1c6aa6576ae9c1c0e7fa2b4883346bc5aa0": "Thirdweb (deprecated)",
    "0x3a70788150c7645a21b95b7062ab1784d3cc2104": "Coinbase (deprecated)",
    "0x3be45f576696a2fd5a93c1330cd19f1607ab311d": "xEcho",
    "0x3f61093f61817b29d9556d3b092e67746af8cdfd": "Heurist",
    "0x40272e2eac848ea70db07fd657d799bd309329c4": "Dexter (deprecated)",
    "0x402feee072d655b85e08f1751af9ddbcd249521f": "Dexter",
    "0x42dd53906b49c202e8e934b059dc019e04634b00": "Coinbase",
    "0x4544b535938b67d2a410a98a7e3b0f8f68921ca7": "Questflow",
    "0x4638bc811c93bf5e60deed32325e93505f681576": "Questflow",
    "0x47d8b3c9717e976f31025089384f23900750a5f4": "Coinbase (deprecated)",
    "0x489c40fc3c2a19ad8cb275b7dd6aa194e9219c4f": "PayAI (deprecated)",
    "0x48ab4b0af4ddc2f666a3fcc43666c793889787a3": "Heurist",
    "0x4c934c63c786157fefd990945b25ea60a0fb0205": "Coinbase",
    "0x4ffeffa616a1460570d1eb0390e264d45a199e91": "Coinbase (deprecated)",
    "0x51fec16843e49b99aaf9814e525aee1756e66a62": "x402 Jobs",
    "0x552300992857834c0ad41c8e1a6934a5e4a2e4ca": "Coinbase (deprecated)",
    "0x59b7ebc67a3d627fabaf06768c818638452ae704": "Coinbase",
    "0x59e8014a3b884392fbb679fe461da07b18c1ff81": "Questflow",
    "0x5e437bee4321db862ac57085ea5eb97199c0ccc5": "X402rs",
    "0x612d72dc8402bba997c61aa82ce718ea23b2df5d": "Heurist",
    "0x625d8a65134079f8faaac39a7947c73d93c6ac39": "Coinbase",
    "0x64cc42b1ce598e3abcfbb64df4688521ddbf1f0a": "Coinbase",
    "0x65058cf664d0d07f68b663b0d4b4f12a5e331a38": "CodeNut",
    "0x66c40946b0dffd04be467e18309857307ecd37cb": "Polymer",
    "0x675707bc7d03089f820c1b7d49f7480083e8f4df": "PayAI (deprecated)",
    "0x67b9ce703d9ce658d7c4ac3c289cea112fe662af": "Coinbase",
    "0x6831508455a716f987782a1ab41e204856055cc2": "Coinbase (deprecated)",
    "0x68a96f41ff1e9f2e7b591a931a4ad224e7c07863": "Coinbase",
    "0x68efafe862d89ce66dd3d7b07d5a3747a0871164": "Coinbase",
    "0x6ccf245c883f9f3c6caee0687aa61daf7bc96e32": "PayAI (deprecated)",
    "0x708e57b6650a9a741ab39cae1969ea1d2d10eca1": "Coinbase (deprecated)",
    "0x724efafb051f17ae824afcdf3c0368ae312da264": "Questflow",
    "0x73b2b8df52fbe7c40fe78db52e3dffdd5db5ad07": "402104",
    "0x76eee8f0acabd6b49f1cc4e9656a0c8892f3332e": "X402rs (deprecated)",
    "0x772003a2e9c2ccc8af956870a37a66f64f8cec38": "Coinbase",
    "0x7c766f5fd9ab3dc09acad5ecfacc99c4781efe29": "OpenFacilitator",
    "0x7e20b62bf36554b704774afb0fcc0ae8f899213b": "Thirdweb (deprecated)",
    "0x7f6d822467df2a85f792d4508c5722ade96be056": "Coinbase (deprecated)",
    "0x7f72a02c682e908d46a5677fe937cdb612d94a3b": "FluxA",
    "0x80735b3f7808e2e229ace880dbe85e80115631ca": "Virtuals Protocol",
    "0x80c08de1a05df2bd633cf520754e40fde3c794d3": "Thirdweb",
    "0x87af99356d774312b73018b3b6562e1ae0e018c9": "CodeNut",
    "0x88800e08e20b45c9b1f0480cf759b5bf2f05180c": "Coinbase (deprecated)",
    "0x88e13d4c764a6c840ce722a0a3765f55a85b327e": "CodeNut",
    "0x8cda367232d78c067116e3260da881d2da8ffa39": "Coinbase",
    "0x8d8fa42584a727488eeb0e29405ad794a105bb9b": "CodeNut",
    "0x8e7769d440b3460b92159dd9c6d17302b036e2d6": "Meridian (deprecated)",
    "0x8f5cb67b49555e614892b7233cfddebfb746e531": "Coinbase",
    "0x90d5e567017f6c696f1916f4365dd79985fce50f": "Heurist (deprecated)",
    "0x90da501fdbec74bb0549100967eb221fed79c99b": "Questflow",
    "0x91d313853ad458addda56b35a7686e2f38ff3952": "Coinbase (deprecated)",
    "0x91ddea05f741b34b63a7548338c90fc152c8631f": "Thirdweb (deprecated)",
    "0x93f6601151ccb08f333ab4b1cccfb1e188c0be44": "Coinbase",
    "0x94701e1df9ae06642bf6027589b8e05dc7004813": "Coinbase (deprecated)",
    "0x97316fa4730bc7d3b295234f8e4d04a0a4c093e8": "OpenX402",
    "0x97acce27d5069544480bde0f04d9f47d7422a016": "Coinbase",
    "0x97d38aa5de015245dcca76305b53abe6da25f6a5": "X402rs",
    "0x97db9b5291a218fc77198c285cefdc943ef74917": "OpenX402 (deprecated)",
    "0x9aae2b0d1b9dc55ac9bab9556f9a26cb64995fb9": "Coinbase (deprecated)",
    "0x9c09faa49c4235a09677159ff14f17498ac48738": "Coinbase (deprecated)",
    "0x9df61a719ddae27c20a63a417271cc2c704654bd": "PayAI (deprecated)",
    "0x9fb2714af0a84816f5c6322884f2907e33946b88": "Coinbase (deprecated)",
    "0xa1822b21202a24669eaf9277723d180cd6dae874": "Thirdweb (deprecated)",
    "0xa32ccda98ba7529705a059bd2d213da8de10d101": "Coinbase",
    "0xa9a54ef09fc8b86bc747cec6ef8d6e81c38c6180": "Questflow",
    "0xaa0df01e4d11decf2ad2c459c81d3a495e4f1925": "FluxA",
    "0xaaca1ba9d2627cbc0739ba69890c30f95de046e4": "Thirdweb (deprecated)",
    "0xadd5585c776b9b0ea77e9309c1299a40442d820f": "Coinbase (deprecated)",
    "0xaf990eef9846b63d896056050fdc0b28bca9c24b": "PayAI (deprecated)",
    "0xb2bd29925cbbcea7628279c91945ca5b98bf371b": "PayAI",
    "0xb578b7db22581507d62bdbeb85e06acd1be09e11": "Heurist",
    "0xb5d25e1fa0718bf3e1bf698f96791d4e93632ec8": "FluxA",
    "0xb70c4fe126de09bd292fe3d1e40c6d264ca6a52a": "AurraCloud",
    "0xb87e1a2cc2b4643f2892768e80e41167f17c5860": "Coinbase",
    "0xb8f41cb13b1f213da1e94e1b742ec1323235c48f": "PayAI",
    "0xc19829b32324f116ee7f80d193f99e445968499a": "X402rs",
    "0xc6699d2aada6c36dfea5c248dd70f9cb0235cb63": "PayAI",
    "0xc67b555b4a9d340ed7c5d87743163c31a75f2254": "FluxA",
    "0xca5e87f82b3fa093800e6ad67d621a427d79c70d": "Coinbase",
    "0xcbb10c30a9a72fae9232f41cbbd566a097b4e03a": "Coinbase (deprecated)",
    "0xce7819f0b0b871733c933d1f486533bab95ec47b": "Questflow",
    "0xce82eeec8e98e443ec34fda3c3e999cbe4cb6ac2": "Coinbase (deprecated)",
    "0xd2f74a14522d40e4a1d7fbb62aa97ce99fa1a7e5": "FluxA",
    "0xd348e724e0ef36291a28dfeccf692399b0e179f8": "AurraCloud",
    "0xd744494e28b01073514ebc89987b305001ed257a": "Obol",
    "0xd7469bf02d221968ab9f0c8b9351f55f8668ac4f": "Coinbase (deprecated)",
    "0xd7d91a42dfadd906c5b9ccde7226d28251e4cd0f": "Questflow",
    "0xd88a9a58806b895ff06744082c6a20b9d7184b0f": "Thirdweb (deprecated)",
    "0xd8dfc729cbd05381647eb5540d756f4f8ad63eec": "X402rs (deprecated)",
    "0xd97c12726dcf994797c981d31cfb243d231189fb": "Heurist",
    "0xdbdf3d8ed80f84c35d01c6c9f9271761bad90ba6": "Coinbase (deprecated)",
    "0xdc8fbad54bf5151405de488f45acd555517e0958": "Coinbase (deprecated)",
    "0xe07e9cbf9a55d02e3ac356ed4706353d98c5a618": "Treasure",
    "0xe299c486066739c4a31609e1268d93229632dd47": "PayAI (deprecated)",
    "0xe575fa51af90957d66fab6d63355f1ed021b887b": "PayAI (deprecated)",
    "0xe6123e6b389751c5f7e9349f3d626b105c1fe618": "Questflow",
    "0xe72f0af4cf41356d433723547f1412ca27fbb1b8": "Coinbase",
    "0xe74817f4cdc15844314812b2271276e64e890fae": "Coinbase",
    "0xea52f2c6f6287f554f9b54c5417e1e431fe5710e": "Thirdweb (deprecated)",
    "0xec10243b54df1a71254f58873b389b7ecece89c2": "Thirdweb (deprecated)",
    "0xf46833d4ac4f0f1405cc05c30edfd86770f721c9": "PayAI (deprecated)",
    "0xf70e7cb30b132fab2a0a5e80d41861aa133ea21b": "Questflow",
    "0xfe0920a0a7f0f8a1ec689146c30c3bbef439bf8a": "Mogami",
}

BUCKETS = [(1, 1, "1"), (2, 2, "2"), (3, 5, "3–5"), (6, 20, "6–20"), (21, 10**9, "21+")]
PROBER_FANOUT_MAX = 1.5      # legs ÷ distinct recipients at or below this = prober-shaped
HEAVY_LEGS = 50              # "heavy buyer" threshold inside the ≥3-recipient set


# ── env ──────────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(os.path.dirname(here), ".env"), os.path.join(here, ".env")):
        try:
            with open(path) as fh:
                for raw in fh:
                    s = raw.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, _, v = s.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except FileNotFoundError:
            continue


# ── Dune ─────────────────────────────────────────────────────────────────────

def dune_run(query_id: int, params: dict, api_key: str, use_latest: bool = False,
             page: int = 30000, timeout_s: int = 900) -> list[dict]:
    """Execute a saved query (or fetch its latest result) and return all rows."""
    if httpx is None:
        sys.exit("httpx missing — run from the repo venv")
    h = {"X-DUNE-API-KEY": api_key}
    with httpx.Client(timeout=120.0) as c:
        if use_latest:
            base = f"{DUNE_API_BASE}/query/{query_id}/results"
        else:
            body: dict = {"performance": "medium"}
            if params:
                body["query_parameters"] = params
            r = c.post(f"{DUNE_API_BASE}/query/{query_id}/execute", headers=h, json=body)
            r.raise_for_status()
            ex = r.json()["execution_id"]
            t0 = time.time()
            while True:
                s = c.get(f"{DUNE_API_BASE}/execution/{ex}/status", headers=h).json()
                st = s.get("state")
                if st == "QUERY_STATE_COMPLETED":
                    break
                if st in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
                    sys.exit(f"Dune execution {ex} ended in {st}: {s.get('error')}")
                if time.time() - t0 > timeout_s:
                    sys.exit(f"Dune execution {ex} still {st} after {timeout_s}s")
                print(f"  … {st}", file=sys.stderr)
                time.sleep(5)
            base = f"{DUNE_API_BASE}/execution/{ex}/results"
        rows: list[dict] = []
        offset = 0
        while True:
            r = c.get(base, headers=h, params={"limit": page, "offset": offset})
            r.raise_for_status()
            j = r.json()
            chunk = j.get("result", {}).get("rows", [])
            rows.extend(chunk)
            meta = j.get("result", {}).get("metadata", {})
            total = meta.get("total_row_count")
            offset += len(chunk)
            if not chunk or len(chunk) < page or (total is not None and offset >= total):
                break
        return rows


def read_csv(path: str) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


# ── analysis ─────────────────────────────────────────────────────────────────

def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _i(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _lower(a) -> str:
    return (a or "").lower()


def _short(a: str, full: bool) -> str:
    a = a or ""
    return a if full or len(a) < 12 else f"{a[:6]}…{a[-4:]}"


def _median(xs: Iterable[float]) -> float:
    xs = list(xs)
    return statistics.median(xs) if xs else 0.0


def _bucket(n: int) -> str:
    for lo, hi, label in BUCKETS:
        if lo <= n <= hi:
            return label
    return "21+"


def fetch_known_paytos() -> set[str]:
    """Distinct pay_to from service_probes (public SELECT) + our own gateway."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    known = {OUR_PAYTO}
    if not (url and key and httpx):
        return known
    try:
        h = {"apikey": key, "Authorization": f"Bearer {key}"}
        off = 0
        with httpx.Client(timeout=60.0) as c:
            while True:
                r = c.get(f"{url.rstrip('/')}/rest/v1/service_probes",
                          headers={**h, "Range": f"{off}-{off + 999}"},
                          params={"select": "pay_to", "pay_to": "not.is.null"})
                if r.status_code not in (200, 206):
                    break
                rows = r.json()
                known.update(_lower(x.get("pay_to")) for x in rows if x.get("pay_to"))
                if len(rows) < 1000:
                    break
                off += 1000
    except Exception as e:  # best-effort
        print(f"  (service_probes lookup skipped: {e})", file=sys.stderr)
    return known


def analyse(payers: list[dict], pairs: list[dict], days: int, full: bool,
            sellers: list[dict] | None = None) -> tuple[str, dict]:
    # normalise
    P = []
    self_rows = []
    for r in payers:
        row = {
            "payer": _lower(r.get("payer")),
            "legs": _i(r.get("legs")),
            "usd": _f(r.get("usd")),
            "recipients": _i(r.get("recipients")),
            "relayers": _i(r.get("relayers")),
            "top_relayer": _lower(r.get("top_relayer")),
            "first_leg": r.get("first_leg"),
            "last_leg": r.get("last_leg"),
        }
        (self_rows if row["payer"] in SELF_PAYERS else P).append(row)

    total_w = len(P)
    total_usd = sum(r["usd"] for r in P)
    total_legs = sum(r["legs"] for r in P)

    # buckets
    by_b: dict[str, list[dict]] = defaultdict(list)
    for r in P:
        by_b[_bucket(r["recipients"])].append(r)

    # ≥3 population
    multi = [r for r in P if r["recipients"] >= 3]
    probers = [r for r in multi if r["legs"] / max(r["recipients"], 1) <= PROBER_FANOUT_MAX]
    buyers = [r for r in multi if r not in probers]
    heavy = [r for r in buyers if r["legs"] >= HEAVY_LEGS]

    # relayer distribution (approx: each payer's legs attributed to its top relayer)
    relayer_legs: Counter = Counter()
    for r in P:
        relayer_legs[r["top_relayer"]] += r["legs"]

    # pairs → per-payer recipient lists (only for multi-recipient payers)
    rec_by_payer: dict[str, list[dict]] = defaultdict(list)
    for pr in pairs:
        p = _lower(pr.get("payer"))
        if p in SELF_PAYERS:
            continue
        rec_by_payer[p].append({
            "recipient": _lower(pr.get("recipient")),
            "legs": _i(pr.get("legs")),
            "usd": _f(pr.get("usd")),
        })

    # provider resolution on the buyers' recipients
    known = fetch_known_paytos()
    buyer_set = {r["payer"] for r in buyers}
    rec_usd: Counter = Counter()
    rec_wallets: set[str] = set()
    for p, recs in rec_by_payer.items():
        if p not in buyer_set:
            continue
        for x in recs:
            rec_usd[x["recipient"]] += x["usd"]
            rec_wallets.add(x["recipient"])
    resolved_w = sum(1 for a in rec_wallets if a in known)
    resolved_usd = sum(u for a, u in rec_usd.items() if a in known)
    buyers_rec_usd = sum(rec_usd.values())

    # our own position
    our_payers = [p for p, recs in rec_by_payer.items() if any(x["recipient"] == OUR_PAYTO for x in recs)]
    our_from_multi = [p for p in our_payers if p in buyer_set]

    # ── report ───────────────────────────────────────────────────────────────
    today = dt.date.today().isoformat()
    L: list[str] = []
    L.append(f"# Multi-provider buyer census — Base, last {days} days ({today})")
    L.append("")
    L.append("**AGE-140 · internal · reviews/ is untracked — do not commit. Addresses "
             + ("shown in full." if full else "truncated; run with --full-addresses for internal use.") + "**")
    L.append("")
    L.append("## Denominator (v2, x402-scoped)")
    L.append("")
    L.append(f"USDC transfers on Base in the last {days} days that (1) were settled through an "
             "**EIP-3009 authorization** (`AuthorizationUsed` emitted by the USDC contract — the x402 "
             "\"exact\"-scheme mechanism), (2) were **submitted by one of the 128 Base relayer addresses in "
             "x402scan's open facilitator registry** (Coinbase 40, PayAI 15, Questflow, Thirdweb, Heurist, "
             "FluxA, X402rs, … — Merit-Systems/x402scan), and (3) went to a **metered seller**: a recipient "
             "with ≥5 distinct payers in the window and a mean leg ≤ $1, i.e. something being paid per API "
             "call rather than receiving purchases. Filter (3) exists because the CDP relayers also submit "
             "Base Pay / consumer gasless USDC sends — the unscoped EIP-3009 set was ~$35M / 10.4M legs / "
             "65k wallets in 30d, ~25x the x402 market as x402scan and fuchss measure it. **Deliberately "
             "excluded:** Bitrefill-style commerce ($136/payer), $1+ inference sellers (Cluster, CheapTokens), "
             "sellers with <5 payers, and Solana. Wallet ≠ operator (rotation reads as N single-recipient "
             "wallets; fanout shape partially corrects, the funding graph — AGE-133 — would fully correct). "
             "Our own flagship and prober wallets are excluded from the population and reported separately.")
    L.append("")
    L.append("## Headline")
    L.append("")
    pct = lambda a, b: (100.0 * a / b) if b else 0.0
    m_usd = sum(r["usd"] for r in multi)
    b_usd = sum(r["usd"] for r in buyers)
    p_usd = sum(r["usd"] for r in probers)
    h_usd = sum(r["usd"] for r in heavy)
    L.append(f"- **{total_w:,} payer wallets** sent **${total_usd:,.2f}** over **{total_legs:,} settlements**.")
    L.append(f"- **{len(multi):,} wallets ({pct(len(multi), total_w):.1f}%) paid ≥3 distinct providers**; "
             f"they sent **${m_usd:,.2f} ({pct(m_usd, total_usd):.1f}% of dollars)**.")
    L.append(f"- Inside that set: **{len(buyers):,} look like buyers** (legs ÷ recipients > {PROBER_FANOUT_MAX}) "
             f"carrying **${b_usd:,.2f} ({pct(b_usd, total_usd):.1f}%)**; "
             f"**{len(probers):,} look like probers** (≈1 payment per recipient) carrying ${p_usd:,.2f} "
             f"({pct(p_usd, total_usd):.1f}%).")
    L.append(f"- **{len(heavy):,} heavy multi-provider buyers** (≥{HEAVY_LEGS} settlements, ≥3 providers) "
             f"carry **${h_usd:,.2f} ({pct(h_usd, total_usd):.1f}%)**.")
    if rec_wallets:
        L.append(f"- Provider resolution on the buyers' recipients: **{resolved_w}/{len(rec_wallets)} wallets "
                 f"({pct(resolved_w, len(rec_wallets)):.0f}%)** and **{pct(resolved_usd, buyers_rec_usd):.0f}% of their dollars** "
                 f"resolve to a payTo we already hold in `service_probes` (+ our own gateway). "
                 "This is the AGE-138 map-coverage baseline.")
    L.append("")
    verdict = (f"{len(multi):,} wallets pay ≥3 providers; they send {pct(m_usd, total_usd):.1f}% of "
               f"x402-scoped Base USDC (facilitator-relayed, metered sellers); of those, {len(buyers):,} look like buyers (fanout ≫1, "
               f"{pct(b_usd, total_usd):.1f}% of dollars) and {len(probers):,} like probers.")
    L.append(f"**Verdict line:** {verdict}")
    L.append("")
    L.append("## By distinct recipients")
    L.append("")
    L.append("| Recipients | Wallets | Wallet % | USD | USD % | Median legs | Median $/leg | Median legs/recipient |")
    L.append("|---|---|---|---|---|---|---|---|")
    for _, _, label in BUCKETS:
        rows = by_b.get(label, [])
        u = sum(r["usd"] for r in rows)
        L.append(f"| {label} | {len(rows):,} | {pct(len(rows), total_w):.1f}% | ${u:,.2f} | {pct(u, total_usd):.1f}% | "
                 f"{_median(r['legs'] for r in rows):.0f} | "
                 f"${_median((r['usd'] / r['legs']) for r in rows if r['legs']):.4f} | "
                 f"{_median((r['legs'] / max(r['recipients'], 1)) for r in rows):.1f} |")
    L.append("")
    L.append("## Relayers (tx senders) — is this set x402?")
    L.append("")
    L.append("Approximation: each payer's settlements attributed to its most-used relayer.")
    L.append("")
    L.append("| Relayer | Label | Legs (approx) | Share |")
    L.append("|---|---|---|---|")
    for addr, n in relayer_legs.most_common(12):
        L.append(f"| `{_short(addr, full)}` | {KNOWN_RELAYERS.get(addr, '(unknown — add to KNOWN_RELAYERS)')} | {n:,} | {pct(n, total_legs):.1f}% |")
    L.append("")
    L.append(f"## Top multi-provider buyers (≥3 providers, fanout > {PROBER_FANOUT_MAX})")
    L.append("")
    L.append("| Payer | Legs | USD | Providers | Legs/provider | Top providers (usd) |")
    L.append("|---|---|---|---|---|---|")
    for r in sorted(buyers, key=lambda x: -x["usd"])[:25]:
        recs = sorted(rec_by_payer.get(r["payer"], []), key=lambda x: -x["usd"])[:3]
        tops = ", ".join(f"`{_short(x['recipient'], full)}` ${x['usd']:.2f}" + (" **(us)**" if x["recipient"] == OUR_PAYTO else "") for x in recs) or "(pairs query not supplied)"
        L.append(f"| `{_short(r['payer'], full)}` | {r['legs']:,} | ${r['usd']:,.2f} | {r['recipients']} | "
                 f"{r['legs'] / max(r['recipients'], 1):.1f} | {tops} |")
    L.append("")
    L.append("## Prober-shaped multi-provider wallets (top 10 by providers)")
    L.append("")
    L.append("| Payer | Legs | USD | Providers | Legs/provider |")
    L.append("|---|---|---|---|---|")
    for r in sorted(probers, key=lambda x: -x["recipients"])[:10]:
        L.append(f"| `{_short(r['payer'], full)}` | {r['legs']:,} | ${r['usd']:,.2f} | {r['recipients']} | "
                 f"{r['legs'] / max(r['recipients'], 1):.2f} |")
    L.append("")

    if sellers:
        S = [{"recipient": _lower(r.get("recipient")), "payers": _i(r.get("payers")), "legs": _i(r.get("legs")),
              "usd": _f(r.get("usd")), "mean_leg": _f(r.get("mean_leg"))} for r in sellers]
        S.sort(key=lambda x: -x["usd"])
        L.append("## Metered sellers (recipients in scope)")
        L.append("")
        L.append(f"{len(S):,} sellers in scope; top 20 by USD. Our gateway is marked.")
        L.append("")
        L.append("| # | Seller payTo | Payers | Legs | USD | Mean $/leg |")
        L.append("|---|---|---|---|---|---|")
        for i, r in enumerate(S[:20], 1):
            L.append(f"| {i} | `{_short(r['recipient'], full)}`{' **(us)**' if r['recipient'] == OUR_PAYTO else ''} | {r['payers']:,} | {r['legs']:,} | ${r['usd']:,.2f} | ${r['mean_leg']:.4f} |")
        ours = [(i, r) for i, r in enumerate(S, 1) if r["recipient"] == OUR_PAYTO]
        if ours:
            i, r = ours[0]
            L.append("")
            L.append(f"AgentPay gateway ranks **#{i} of {len(S):,}** sellers in scope: {r['payers']} payers / {r['legs']} legs / ${r['usd']:.2f}.")
        else:
            L.append("")
            L.append("AgentPay gateway is **not in scope** this window (fewer than 5 distinct payers or mean leg > $1).")
        L.append("")
    L.append("## Our position")
    L.append("")
    if rec_by_payer:
        L.append(f"- {len(our_payers)} payer wallets in the multi-recipient set paid our gateway; "
                 f"{len(our_from_multi)} of them are multi-provider *buyers* (the population the thesis targets).")
    else:
        L.append("- (pairs query not supplied — cannot place our gateway among the buyers' recipients)")
    for r in self_rows:
        L.append(f"- Own wallet `{_short(r['payer'], full)}`: {r['legs']} legs / ${r['usd']:.2f} / {r['recipients']} providers (excluded from population).")
    L.append("")
    L.append("## Reading the result (decision this feeds — AGE-140)")
    L.append("")
    L.append("- If multi-provider **buyers** number in the hundreds and carry a majority of dollars, the "
             "ledger/\"total\" thesis is a real, small, **named** market: go-to-market is the list above, not a funnel.")
    L.append("- If they carry a small share, the ledger is a wallet feature; keep receipts inside the July "
             "session spec and spend the effort on AGE-138 / verified_route.")
    L.append("- Either way the provider-resolution % is the number to move (AGE-138).")
    L.append("")
    L.append("## Caveats")
    L.append("")
    L.append("- Base only; no Solana. Under-counts Ramp/Solana-driven multi-provider agents.")
    L.append("- EIP-3009 filter includes non-x402 gasless USDC sends; check the relayer table. It also misses "
             "direct (self-submitted, non-authorized) x402 settlements — a coverage gap, not a bias toward the thesis.")
    L.append("- Probers that pay twice (replay checks) can look like buyers on 2 legs; the fanout cut is a "
             f"shape heuristic (≤{PROBER_FANOUT_MAX} legs/recipient), not proof.")
    L.append("- Internal only. Public copy gets the shape (percentages, counts), never the wallet list.")
    L.append("")
    L.append(f"*Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by tools/census_multi_provider.py; "
             f"sources: Dune (tools/sql/census_*_x402.sql), x402scan facilitator registry, service_probes (payTo set).*")

    summary = {
        "days": days, "wallets": total_w, "usd": round(total_usd, 2), "legs": total_legs,
        "multi_wallets": len(multi), "multi_usd_pct": round(pct(m_usd, total_usd), 1),
        "buyers": len(buyers), "buyers_usd_pct": round(pct(b_usd, total_usd), 1),
        "probers": len(probers), "heavy_buyers": len(heavy), "heavy_usd_pct": round(pct(h_usd, total_usd), 1),
        "resolved_wallet_pct": round(pct(resolved_w, len(rec_wallets)), 1) if rec_wallets else None,
        "resolved_usd_pct": round(pct(resolved_usd, buyers_rec_usd), 1) if buyers_rec_usd else None,
        "verdict": verdict,
    }
    return "\n".join(L) + "\n", summary


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payers-query-id", type=int, help="Dune query id for tools/sql/census_payers.sql")
    ap.add_argument("--pairs-query-id", type=int, help="Dune query id for tools/sql/census_pairs.sql (optional)")
    ap.add_argument("--payers-csv", help="CSV export of the payers query (alternative to Dune API)")
    ap.add_argument("--pairs-csv", help="CSV export of the pairs query (alternative to Dune API)")
    ap.add_argument("--sellers-query-id", type=int, help="Dune query id for tools/sql/census_sellers_x402.sql (optional)")
    ap.add_argument("--sellers-csv", help="CSV export of the sellers query (optional)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-recipients", type=int, default=2, help="pairs query parameter")
    ap.add_argument("--use-latest", action="store_true", help="fetch the query's latest cached result instead of executing (no credits)")
    ap.add_argument("--no-params", action="store_true",
                    help="saved queries have the window hardcoded (no {{days}}/{{min_recipients}} parameters) — send none")
    ap.add_argument("--full-addresses", action="store_true")
    ap.add_argument("--out", help="report path (default reviews/CENSUS_MULTI_PROVIDER_<date>.md)")
    ap.add_argument("--json", action="store_true", help="also print the summary as JSON")
    a = ap.parse_args()

    _load_dotenv()
    payers: list[dict] = []
    pairs: list[dict] = []

    if a.payers_csv:
        payers = read_csv(a.payers_csv)
    elif a.payers_query_id:
        key = os.environ.get("DUNE_API_KEY")
        if not key:
            sys.exit("DUNE_API_KEY not found in .env")
        print(f"→ Dune payers query {a.payers_query_id} (days={a.days})", file=sys.stderr)
        payers = dune_run(a.payers_query_id, {} if a.no_params else {"days": a.days}, key, a.use_latest)
    else:
        sys.exit("need --payers-query-id or --payers-csv")

    if a.pairs_csv:
        pairs = read_csv(a.pairs_csv)
    elif a.pairs_query_id:
        key = os.environ.get("DUNE_API_KEY")
        print(f"→ Dune pairs query {a.pairs_query_id} (days={a.days}, min_recipients={a.min_recipients})", file=sys.stderr)
        pairs = dune_run(a.pairs_query_id,
                         {} if a.no_params else {"days": a.days, "min_recipients": a.min_recipients},
                         key, a.use_latest)

    sellers: list[dict] = []
    if a.sellers_csv:
        sellers = read_csv(a.sellers_csv)
    elif a.sellers_query_id:
        key = os.environ.get("DUNE_API_KEY")
        print(f"→ Dune sellers query {a.sellers_query_id}", file=sys.stderr)
        sellers = dune_run(a.sellers_query_id, {}, key, a.use_latest)

    print(f"  payers rows: {len(payers):,}   pairs rows: {len(pairs):,}   sellers rows: {len(sellers):,}", file=sys.stderr)
    report, summary = analyse(payers, pairs, a.days, a.full_addresses, sellers or None)

    here = os.path.dirname(os.path.abspath(__file__))
    out = a.out or os.path.join(os.path.dirname(here), "reviews",
                                f"CENSUS_MULTI_PROVIDER_{dt.date.today().isoformat()}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(report)
    print(f"✓ wrote {out}")
    print(summary["verdict"])
    if a.json:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
