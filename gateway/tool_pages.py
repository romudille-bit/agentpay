"""
gateway/tool_pages.py — server-rendered HTML pages for /tools and /tools/{name}.

The JSON responses on these routes are the agent-facing contract and stay the
default. But 20 of the sitemap's URLs used to serve ONLY JSON, so search
engines had nothing to index for queries like "free crypto price API for AI
agents" or "x402 token security tool". routes/tools.py now content-negotiates
(same pattern as the root route): browsers (Accept: text/html, no
application/json) get these pages; agents and tests (Accept: */* or JSON) are
untouched.

Everything a crawler needs is in the returned HTML: title, meta description,
canonical, JSON-LD (schema.org WebAPI), the parameter schema, and working
curl / Python / MCP snippets. No client-side rendering — that's the trap that
kept /probes invisible (see routes/prober.py service_page).
"""

import html as _html
import json as _json

from registry.registry import Tool

_CSS = """
  :root{--bg:#0a0a0b;--card:#131316;--line:#1f1f24;--fg:#e8e8e8;--mut:#8a8a92;--ac:#5eead4;--price:#4ade80}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
  .wrap{max-width:820px;margin:0 auto;padding:28px 18px 60px}
  .crumb{font-size:13px;margin:0 0 14px}.crumb a{color:var(--ac);text-decoration:none}
  h1{font-size:24px;margin:0 0 4px;font-family:"SF Mono",Menlo,Consolas,monospace}
  h2{font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
    color:var(--mut);margin:26px 0 10px}
  .lead{color:var(--mut);font-size:16px;margin:0 0 14px}
  .chips{margin:0 0 18px}.chip{display:inline-block;color:var(--mut);font-size:12px;
    border:1px solid var(--line);border-radius:20px;padding:2px 10px;margin:0 6px 6px 0}
  .chip.price{color:var(--price);border-color:#1f3a2a;font-weight:700}
  pre{background:var(--card);border:1px solid var(--line);border-radius:8px;
    padding:14px 16px;overflow-x:auto;font:13px/1.6 "SF Mono",Menlo,Consolas,monospace}
  table{width:100%;border-collapse:collapse;font-size:14px;margin:0 0 6px}
  th,td{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  td code{background:var(--card);border-radius:4px;padding:1px 5px;font-size:13px}
  a{color:var(--ac)}p{color:#cdd6df}
  .grid{list-style:none;padding:0;margin:0}
  .grid li{display:grid;grid-template-columns:200px 70px 1fr;gap:1.2rem;padding:9px 0;
    border-bottom:1px solid var(--line);align-items:baseline;font-size:14px}
  .grid .n{font-family:"SF Mono",Menlo,Consolas,monospace}
  .grid .p{color:var(--price);font-family:"SF Mono",Menlo,Consolas,monospace}
  .grid .d{color:var(--mut)}
  @media(max-width:640px){.grid li{grid-template-columns:1fr 70px}.grid .d{grid-column:1/-1}}
  .foot{color:var(--mut);font-size:12px;margin-top:28px;border-top:1px solid var(--line);
    padding-top:12px}.foot a{color:var(--ac)}
"""


def _e(s) -> str:
    return _html.escape(str(s if s is not None else ""))


def _price_label(price_usdc: str) -> str:
    try:
        return "Free" if float(price_usdc) == 0 else f"${price_usdc} USDC"
    except (ValueError, TypeError):
        return f"${price_usdc} USDC"


def _foot(gateway_url: str) -> str:
    return (f'<div class="foot"><a href="{gateway_url}/tools">All tools</a> · '
            f'<a href="{gateway_url}/probes">x402 delivery scores</a> · '
            f'<a href="{gateway_url}/ledger">Receipt ledger</a> · '
            f'<a href="{gateway_url}/llms.txt">llms.txt</a> · '
            f'<a href="{gateway_url}/">AgentPay</a></div>')


def render_tool_page(tool: Tool, gateway_url: str) -> str:
    """One indexable page per tool: what it does, price, params, how to call."""
    free = False
    try:
        free = float(tool.price_usdc) == 0
    except (ValueError, TypeError):
        pass
    price = _price_label(tool.price_usdc)

    # Title targets the queries people (and answer engines) actually type:
    # "<thing> API for AI agents", "free <thing> API no key", "x402 <thing>".
    title = (f"{tool.name} — {tool.description} | "
             f"{'free' if free else 'x402'} API for AI agents | AgentPay")
    desc = (f"{tool.description}. "
            + ("Free — no API key, no wallet, no USDC. "
               if free else f"{price} per call via x402 (USDC on Base or Stellar). ")
            + f"Call it via HTTP, the agentpay-x402 Python SDK, or MCP "
              f"(npx @romudille/agentpay-mcp).")

    props = (tool.parameters or {}).get("properties", {}) or {}
    required = set((tool.parameters or {}).get("required", []) or [])
    if props:
        rows = "\n".join(
            f"<tr><td><code>{_e(k)}</code></td>"
            f"<td>{_e(v.get('type', ''))}</td>"
            f"<td>{'yes' if k in required else 'no'}</td>"
            f"<td>{_e(v.get('description', ''))}</td></tr>"
            for k, v in props.items()
        )
        params_html = (f"<h2>Parameters</h2><table><thead><tr><th>name</th><th>type</th>"
                       f"<th>required</th><th>description</th></tr></thead>"
                       f"<tbody>{rows}</tbody></table>")
        example_params = {k: v.get("description", "…") for k, v in props.items()
                          if k in required} or {}
    else:
        params_html = "<h2>Parameters</h2><p>None — call with an empty parameters object.</p>"
        example_params = {}

    example_body = _json.dumps({"parameters": example_params,
                                "agent_address": "<your wallet or any identifier>"})
    curl = (f"curl -X POST {gateway_url}/tools/{tool.name}/call \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{example_body}'"
            + ("" if free else
               "\n# → HTTP 402 challenge → pay USDC on Base (gasless EIP-3009) or Stellar,"
               "\n#   retry with the payment proof header. The SDK below does this for you."))
    py = ("from agentpay import quickstart  # pip install agentpay-x402\n\n"
          "s = quickstart(max_spend=0.10)   # hard budget cap; mints a wallet, no funding needed\n"
          f"r = s.call(\"{tool.name}\", {_json.dumps(example_params)})\n"
          "print(r.data)                    # + r.cost, r.tx, r.network — full receipt")

    returns_html = ""
    if tool.returns:
        returns_html = f"<h2>Returns</h2><p><code>{_e(tool.returns)}</code></p>"
    if tool.response_example is not None:
        returns_html += (f"<h2>Example response</h2>"
                         f"<pre>{_e(_json.dumps(tool.response_example, indent=2))}</pre>")
    use_when_html = (f"<h2>When agents use this</h2><p>{_e(tool.use_when)}</p>"
                     if tool.use_when else "")

    json_ld = _json.dumps({
        "@context": "https://schema.org",
        "@type": "WebAPI",
        "name": tool.name,
        "description": tool.description,
        "url": f"{gateway_url}/tools/{tool.name}",
        "documentation": f"{gateway_url}/llms.txt",
        "provider": {"@type": "Organization", "name": "AgentPay",
                     "url": gateway_url},
        "offers": {"@type": "Offer",
                   "price": "0" if free else str(tool.price_usdc),
                   "priceCurrency": "USD",
                   "description": ("Free — no API key or wallet required"
                                   if free else
                                   f"{price} per call, settled via the x402 protocol")},
    }, indent=1)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(desc)}">
<link rel="canonical" href="{gateway_url}/tools/{_e(tool.name)}">
<meta property="og:title" content="{_e(tool.name)} — {_e(tool.description)}">
<meta property="og:description" content="{_e(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{gateway_url}/tools/{_e(tool.name)}">
<meta property="og:image" content="{gateway_url}/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="{gateway_url}/favicon.svg">
<script type="application/ld+json">{json_ld}</script>
<style>{_CSS}</style></head><body><div class="wrap">
<p class="crumb"><a href="{gateway_url}/tools">← all AgentPay tools</a></p>
<h1>{_e(tool.name)}</h1>
<p class="lead">{_e(tool.description)}.</p>
<div class="chips"><span class="chip price">{_e(price)}</span>
<span class="chip">{_e(tool.category)}</span>
<span class="chip">x402 protocol</span>
{'<span class="chip">no API key · no wallet</span>' if free else '<span class="chip">USDC on Base or Stellar</span>'}</div>
{use_when_html}
{params_html}
{returns_html}
<h2>Call it — HTTP (x402)</h2>
<pre>{_e(curl)}</pre>
<h2>Call it — Python SDK</h2>
<pre>{_e(py)}</pre>
<h2>Call it — MCP</h2>
<pre>npx -y @romudille/agentpay-mcp   # keyless; exposes {_e(tool.name)} to any MCP agent runtime</pre>
{_foot(gateway_url)}
</div></body></html>"""


def render_tools_index(tools: list[Tool], gateway_url: str) -> str:
    """Indexable directory page for /tools — links every tool page."""
    active = [t for t in sorted(tools, key=lambda x: x.name) if t.active]
    n_free = len([t for t in active if float(t.price_usdc) == 0])
    title = (f"{len(active)} crypto & web data tools for AI agents "
             f"({n_free} free, no API keys) | AgentPay")
    desc = (f"All {len(active)} AgentPay tools: market data, DeFi, security, and "
            f"routing APIs AI agents can call over HTTP, Python SDK, or MCP. "
            f"{n_free} are free with no API key or wallet; paid tools cost $0.01 "
            f"via x402 (USDC on Base or Stellar).")
    rows = "\n".join(
        f'<li><span class="n"><a href="{gateway_url}/tools/{_e(t.name)}">{_e(t.name)}</a></span>'
        f'<span class="p">{_e(_price_label(t.price_usdc)).replace(" USDC", "")}</span>'
        f'<span class="d">{_e(t.description)}</span></li>'
        for t in active
    )
    json_ld = _json.dumps({
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "AgentPay tools",
        "description": desc,
        "numberOfItems": len(active),
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1,
             "url": f"{gateway_url}/tools/{t.name}", "name": t.name}
            for i, t in enumerate(active)
        ],
    })
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(desc)}">
<link rel="canonical" href="{gateway_url}/tools">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{gateway_url}/tools">
<meta property="og:image" content="{gateway_url}/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="{gateway_url}/favicon.svg">
<script type="application/ld+json">{json_ld}</script>
<style>{_CSS}</style></head><body><div class="wrap">
<p class="crumb"><a href="{gateway_url}/">← AgentPay</a></p>
<h1 style="font-family:inherit">Tools for AI agents</h1>
<p class="lead">{_e(desc)}</p>
<ul class="grid">
{rows}
</ul>
{_foot(gateway_url)}
</div></body></html>"""
