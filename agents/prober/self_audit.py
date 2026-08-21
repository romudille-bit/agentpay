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


# ── AGE-108 phase 2: timed method-matrix (the AGE-135 instrument) ────────────
# fuchss's EU vantage reads our paid tools' 402s at ~1.5s median with a
# 5s–130s failure tail, while session_create reads 333ms — and a one-off US
# vantage could not reproduce the gap. Nobody on our side was measuring our
# own challenge latency per METHOD and per RESOURCE from outside; this is
# that instrument. Monitoring only — results never enter service_scores.
#
# Every cell is an UNPAID request that must return 402 (AGE-134 pinned that
# for every shape). Failures = wrong status / network error. Warnings =
# latency anomalies (slow samples, or a tools-vs-session differential — the
# AGE-135-class signature) — warnings never fail the audit, they surface in
# the sweep note so a drift is seen the week it starts, not when an external
# scorer prices it in.

import statistics
import time

MATRIX_PATHS = (
    "/v1/session/create",
    "/tools/pre_trade_check/call",
    "/tools/verified_route/call",
)
# The bazaar-declared example bodies — probe what we advertise agents send.
MATRIX_BODIES = {
    "/v1/session/create": {"max_spend": "0.10"},
    "/tools/pre_trade_check/call":
        {"parameters": {"symbol": "ETH", "size_usd": 50000, "side": "long"}},
    "/tools/verified_route/call":
        {"parameters": {"need": "dex pair liquidity", "budget_usd": 1}},
}
MATRIX_METHODS = ("GET", "HEAD", "POST-bare", "POST-json")
SLOW_SAMPLE_S = 5.0          # any single sample slower than this is a warning
DIFFERENTIAL_RATIO = 2.5     # tools median > ratio × session median → warning
_SESSION_PATH = "/v1/session/create"


def _timed_request(url: str, method: str, body: dict | None, timeout: int = 20):
    """One unpaid request, timed to full body read. Returns (status, seconds).

    status is None on a network-level error (DNS, reset, timeout) — exactly
    the shape an external prober records as a hard availability failure.
    """
    if method == "GET":
        req = urllib.request.Request(url, headers={"User-Agent": UA})
    elif method == "HEAD":
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": UA})
    elif method == "POST-bare":
        req = urllib.request.Request(url, method="POST",
                                     headers={"User-Agent": UA})
    else:  # POST-json — the bazaar example body
        req = urllib.request.Request(
            url, data=json.dumps(body or {}).encode(),
            headers={"User-Agent": UA, "Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            r.read()
            return r.status, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, time.monotonic() - t0
    except Exception:
        return None, time.monotonic() - t0


def latency_matrix(gateway: str, samples: int = 3,
                   request=_timed_request) -> dict:
    """Probe MATRIX_PATHS × MATRIX_METHODS × samples, timed. Never raises.

    Returns {ok, failures, warnings, cells}. cells[path][method] =
    {n, ok_402, p50_s, max_s, statuses}. `ok` reflects FAILURES only
    (non-402 / network error); latency anomalies are warnings.
    """
    failures: list[str] = []
    warnings: list[str] = []
    cells: dict = {}

    for path in MATRIX_PATHS:
        url = f"{gateway}{path}"
        cells[path] = {}
        for method in MATRIX_METHODS:
            times: list[float] = []
            statuses: list = []
            ok_402 = 0
            for _ in range(samples):
                status, secs = request(url, method, MATRIX_BODIES.get(path))
                statuses.append(status)
                times.append(secs)
                if status == 402:
                    ok_402 += 1
                else:
                    failures.append(
                        f"latency_matrix: {method} {path} → "
                        f"{status if status is not None else 'network-error'} "
                        f"(expected 402) in {secs:.2f}s")
                if secs > SLOW_SAMPLE_S:
                    warnings.append(
                        f"latency_matrix: {method} {path} sample took "
                        f"{secs:.2f}s (> {SLOW_SAMPLE_S:.0f}s) — external "
                        f"probers score this band as a failure")
            cells[path][method] = {
                "n": samples, "ok_402": ok_402,
                "p50_s": round(statistics.median(times), 3),
                "max_s": round(max(times), 3),
                "statuses": statuses,
            }

    # The AGE-135-class signature: a paid tool's challenge consistently slower
    # than session_create's for the same method. Same vantage, same moment —
    # a ratio here is a real differential, not an RTT artifact.
    for method in MATRIX_METHODS:
        base = cells.get(_SESSION_PATH, {}).get(method, {}).get("p50_s")
        if not base or base <= 0:
            continue
        for path in MATRIX_PATHS:
            if path == _SESSION_PATH:
                continue
            p50 = cells.get(path, {}).get(method, {}).get("p50_s")
            if p50 and p50 > base * DIFFERENTIAL_RATIO and p50 - base > 0.5:
                warnings.append(
                    f"latency_matrix: {method} {path} p50 {p50:.2f}s vs "
                    f"session_create {base:.2f}s ({p50 / base:.1f}x) — "
                    f"AGE-135-class differential, investigate before an "
                    f"external scorer prices it in")

    return {"ok": not failures, "failures": failures,
            "warnings": warnings, "cells": cells, "samples": samples}


def summarize_matrix(result: dict) -> str:
    """One line for the log / ledger note."""
    cells = result.get("cells", {})
    total = sum(c["n"] for m in cells.values() for c in m.values())
    ok = sum(c["ok_402"] for m in cells.values() for c in m.values())
    worst = max((c["max_s"] for m in cells.values() for c in m.values()),
                default=0.0)
    bits = [f"latency matrix {ok}/{total} 402s, worst sample {worst:.2f}s"]
    if result.get("warnings"):
        bits.append(f"{len(result['warnings'])} latency warning(s)")
    if not result.get("ok"):
        bits.append(f"{len(result.get('failures', []))} FAILURE(S)")
    return " — ".join(bits)


if __name__ == "__main__":  # instant on-demand health read
    import sys as _sys
    _gw = _sys.argv[1] if len(_sys.argv) > 1 else "https://agentpay.tools"
    _r = audit(_gw)
    print(summarize(_r))
    for _f in _r["failures"]:
        print(" !", _f)
    _m = latency_matrix(_gw)
    print(summarize_matrix(_m))
    for _f in _m["failures"]:
        print(" !", _f)
    for _w in _m["warnings"]:
        print(" ~", _w)
    for _p, _methods in _m["cells"].items():
        for _meth, _c in _methods.items():
            print(f"   {_p:34s} {_meth:9s} p50={_c['p50_s']}s "
                  f"max={_c['max_s']}s ok={_c['ok_402']}/{_c['n']}")
