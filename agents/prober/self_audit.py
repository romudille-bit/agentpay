"""AGE-108: audit OUR OWN discovery surface on every prober sweep.

The Prober never scores our own tools — that fairness rule is right and stays.
But "never score" quietly became "never look", and two contract bugs shipped
unnoticed until an outside agent reported one (AGE-107, AGE-112). This is
monitoring, not scoring: nothing here touches service_scores or any ranking.

Free and read-only. Unpaid POSTs stop at the 402 challenge; nothing settles.

Checks, each mapped to a bug it would have caught:
  discovery_fields  every active tool has endpoint/description/price/params  (AGE-107)
  canonical_402     all payable paths for a tool declare the SAME resource   (AGE-112)
  unknown_404       a bogus tool name 404s — negative control
  well_known        llms.txt / manifests / sitemap reachable

Two things learned building this, both worth keeping in mind when editing:
the resource block lives ONLY in the base64 PAYMENT-REQUIRED header, not in
the 402 body; and a tool's registry `endpoint` is its info page, which for
most tools is GET-only and NOT the payable path.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

UA = "agentpay-self-audit/1.0 (+https://agentpay.tools)"
REQUIRED_TOOL_FIELDS = ("endpoint", "description", "price_usdc", "parameters")
WELL_KNOWN = ("/llms.txt", "/.well-known/agentpay.json",
              "/.well-known/agent.json", "/sitemap.xml")
BOGUS_TOOL = "zzz-self-audit-not-a-tool"


def _get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return None, str(e)


def _post(url: str, body: dict, timeout: int = 20):
    """Unpaid POST — expected to come back 402. Returns (status, body, headers)."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), dict(e.headers or {})
    except Exception as e:
        return None, str(e), {}


def resource_from_header(headers: dict) -> dict:
    """Decode `resource` out of the base64 PAYMENT-REQUIRED header."""
    raw = ""
    for k, v in (headers or {}).items():
        if k.lower() == "payment-required":
            raw = (v or "").strip()
            break
    if not raw:
        return {}
    try:
        payload = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
        return payload.get("resource") or {}
    except Exception:
        return {}


def audit(gateway: str, get=_get, post=_post) -> dict:
    """Run every check. Returns {ok, failures, checks} — never raises."""
    failures: list[str] = []
    checks: dict = {}

    # ── /tools completeness (AGE-107) ────────────────────────────────────────
    status, raw = get(f"{gateway}/tools")
    tools = []
    if status != 200:
        failures.append(f"/tools returned {status}")
    else:
        try:
            tools = (json.loads(raw) or {}).get("tools", [])
        except Exception as e:
            failures.append(f"/tools not JSON: {e}")

    incomplete = []
    for t in tools:
        if not t.get("active", True):
            continue
        missing = [f for f in REQUIRED_TOOL_FIELDS if not t.get(f)]
        if missing:
            incomplete.append({"tool": t.get("name"), "missing": missing})
            failures.append(f"{t.get('name')} missing {missing}")
    checks["discovery_fields"] = {"tools": len(tools), "incomplete": incomplete}

    # ── paid tools: every payable path declares the same resource (AGE-112) ──
    report = []
    for t in tools:
        try:
            priced = float(t.get("price_usdc") or 0) > 0
        except (TypeError, ValueError):
            priced = False
        if not priced:
            continue
        name = t.get("name")
        # Candidates; a path that doesn't 402 simply isn't payable (the info
        # page 405s) and is skipped rather than reported.
        candidates = {f"{gateway}/tools/{name}/call"}
        if t.get("endpoint"):
            candidates.add(t["endpoint"])

        payable = []
        for url in sorted(candidates):
            code, body, headers = post(url, {"parameters": {}})
            if code != 402:
                continue
            res = resource_from_header(headers)
            try:
                accepts = (json.loads(body) or {}).get("accepts") or []
            except Exception:
                accepts = []
            payable.append({"tool": name, "path": url,
                            "serviceName": res.get("serviceName"),
                            "resource_url": res.get("url")})
            if not res.get("serviceName"):
                failures.append(f"{name} at {url}: 402 declares no serviceName")
            if not res.get("url"):
                failures.append(f"{name} at {url}: 402 declares no resource url")
            if not accepts:
                failures.append(f"{name} at {url}: accepts[] empty")
            else:
                a = accepts[0]
                for k in ("payTo", "network"):
                    if not a.get(k):
                        failures.append(f"{name} at {url}: accepts[0].{k} missing")
                if not (a.get("amount") or a.get("maxAmountRequired")):
                    failures.append(f"{name} at {url}: accepts[0] has no amount")

        if not payable:
            failures.append(f"{name}: no payable path returned a 402")
        # The AGE-112 bug in one assertion: disagreeing paths for one product.
        urls = {p["resource_url"] for p in payable if p["resource_url"]}
        names = {p["serviceName"] for p in payable if p["serviceName"]}
        if len(urls) > 1:
            failures.append(
                f"{name}: payable paths declare different resources {sorted(urls)} "
                f"— a settle on one will not refresh the other")
        if len(names) > 1:
            failures.append(f"{name}: payable paths declare different "
                            f"serviceNames {sorted(names)}")
        report.extend(payable)
    checks["canonical_402"] = report

    # ── negative control ─────────────────────────────────────────────────────
    code, _ = get(f"{gateway}/tools/{BOGUS_TOOL}")
    checks["unknown_404"] = code
    if code != 404:
        failures.append(f"unknown tool name returned {code}, expected 404 "
                        f"— discovery may be answering for anything")

    # ── well-known surfaces ──────────────────────────────────────────────────
    wk = {}
    for path in WELL_KNOWN:
        code, _ = get(f"{gateway}{path}")
        wk[path] = code
        if code != 200:
            failures.append(f"{path} returned {code}")
    checks["well_known"] = wk

    return {"ok": not failures, "failures": failures, "checks": checks}


def summarize(result: dict) -> str:
    """One line for the ledger note."""
    if result.get("ok"):
        c = result.get("checks", {})
        return (f"self-audit OK — {c.get('discovery_fields', {}).get('tools', 0)} "
                f"tools complete, {len(c.get('canonical_402', []))} payable "
                f"paths canonical")
    fails = result.get("failures", [])
    return f"SELF-AUDIT FAILED ({len(fails)}): " + "; ".join(fails[:3])
