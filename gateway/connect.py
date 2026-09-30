"""
gateway/connect.py — connector install pages: one remote MCP URL, four harnesses.

/connect and /connect/<harness> tell a user of claude.ai, ChatGPT, Perplexity
or Grok how to add https://agentpay.tools/mcp as a custom connector. Same
visual system as the guides; the per-harness facts were checked against each
vendor's own docs on the date shown. Add a harness: append to HARNESSES.
"""
import json

from gateway.tool_pages import _CSS, _e, _foot

VERIFIED = "2026-09-30"

FIRST_PROMPT = ('Use AgentPay verified_route to find a real, actually-used x402 provider for '
                '"dex pair liquidity" with a $0.01 budget, and tell me why it picked that one.')

HARNESSES: dict[str, dict] = {
    "claude": {
        "name": "claude.ai (web + mobile)",
        "short": "claude.ai",
        "blurb": "Custom connector, no auth. Also works in Claude Desktop.",
        "plans": "Pro, Max, Team and Enterprise plans.",
        "steps": [
            "Open Settings → Connectors.",
            "Click <b>Add custom connector</b> at the bottom of the section.",
            "Name it <b>AgentPay</b> and paste the URL below. Leave the OAuth fields empty.",
            "Click <b>Add</b>.",
            "In a chat, use the <b>+</b> button → Connectors to enable AgentPay for that conversation.",
        ],
        "notes": [
            "Same URL works on the phone app once it is added from the web settings.",
            "Claude Desktop and Claude Code can also run the local server with a wallet: see the local path below.",
        ],
        "source": "https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp",
    },
    "chatgpt": {
        "name": "ChatGPT",
        "short": "ChatGPT",
        "blurb": "Developer mode → custom MCP. Our keyless tool set is read-only, so Pro is enough.",
        "plans": "Pro (read/fetch tools), Business, Enterprise and Edu.",
        "steps": [
            "Settings → Apps → Advanced settings → turn on <b>Developer mode</b>. "
            "(Enterprise/Edu: an admin first allows it under Permissions &amp; Roles → Connected Data.)",
            "Apps → <b>Create</b>.",
            "Name <b>AgentPay</b>, endpoint URL below, authentication <b>None</b>.",
            "Click <b>Scan Tools</b> — the 22 tools appear.",
        ],
        "notes": [
            "Pro accounts get read/fetch tools only. Every AgentPay remote tool is read-only "
            "(<code>readOnlyHint: true</code>), so nothing is missing on Pro.",
        ],
        "source": "https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt",
    },
    "perplexity": {
        "name": "Perplexity",
        "short": "Perplexity",
        "blurb": "Custom remote connector, Streamable HTTP, auth None.",
        "plans": "Accounts with custom remote connectors enabled.",
        "steps": [
            "Settings → Connectors → add a <b>custom remote connector</b>.",
            "Name <b>AgentPay</b>, URL below, transport <b>Streamable HTTP</b>, authentication <b>None</b>.",
            "Save, then enable it in the conversation's connector picker.",
        ],
        "notes": [
            "Perplexity's Mac app also runs local MCP servers; the remote URL is the one that works everywhere.",
        ],
        "source": "https://www.perplexity.ai/help-center/en/articles/13915507-adding-custom-remote-connectors",
    },
    "grok": {
        "name": "Grok",
        "short": "Grok",
        "blurb": "grok.com/connectors → Custom → paste the URL; Grok discovers the tools.",
        "plans": "All users; Business and Enterprise workspaces need an admin to provision it.",
        "steps": [
            "Go to <a href=\"https://grok.com/connectors\">grok.com/connectors</a>.",
            "Choose <b>Custom</b> and paste the URL below.",
            "Grok discovers the tools; enable the connector in a chat.",
        ],
        "notes": [],
        "source": "https://docs.x.ai/grok/connectors",
    },
}


def _json_ld(slug: str, h: dict, gateway_url: str) -> str:
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "HowTo",
        "name": f"Add AgentPay to {h['name']}",
        "description": f"Connect the AgentPay remote MCP ({gateway_url}/mcp) in {h['name']}.",
        "url": f"{gateway_url}/connect/{slug}",
        "step": [{"@type": "HowToStep", "position": i + 1,
                  "text": _strip(s)} for i, s in enumerate(h["steps"])],
        "author": {"@type": "Organization", "name": "AgentPay", "url": gateway_url},
    }, separators=(",", ":"))


def _strip(html: str) -> str:
    out, keep = [], True
    for ch in html:
        if ch == "<":
            keep = False
        elif ch == ">":
            keep = True
        elif keep:
            out.append(ch)
    return "".join(out).replace("&amp;", "&")


_EXTRA_CSS = """
.url{display:block;margin:10px 0 18px;padding:12px 14px;background:#1a2128;border:1px solid var(--line);
     border-radius:8px;font:15px ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.mut{color:var(--mut)}
.meta{color:var(--mut);font-size:.9rem}
.tabs a{display:inline-block;margin:0 8px 8px 0;padding:6px 12px;border:1px solid var(--line);
        border-radius:999px;color:var(--fg);text-decoration:none}
.tabs a.on{border-color:var(--ac);color:var(--ac)}
ol li{margin:6px 0}
.note{border-left:3px solid var(--line);padding-left:14px;color:var(--mut)}
pre{white-space:pre-wrap}
"""


def _tabs(gateway_url: str, current: str | None) -> str:
    links = []
    for s, h in HARNESSES.items():
        cls = ' class="on"' if s == current else ""
        links.append(f'<a href="{gateway_url}/connect/{_e(s)}"{cls}>{_e(h["short"])}</a>')
    return '<p class="tabs">' + "".join(links) + "</p>"


def _common(gateway_url: str) -> str:
    return f"""
<h2>What you get</h2>
<p>The keyless AgentPay tool set: 17 free crypto and market-data tools, the <code>verified_route</code>
trust preview (which x402 provider for a need is real and actually used), and <code>estimate_plan</code>
(price a multi-tool plan before spending). No account, no key, nothing to fund.</p>
<h2>First thing to try</h2>
<pre>{_e(FIRST_PROMPT)}</pre>
<h2>Paid verdicts</h2>
<p class="note">The remote server never holds a wallet, so the three $0.01 tools
(<code>pre_trade_check</code>, paid <code>verified_route</code>, <code>session_create</code>) answer with the
x402 quote instead of settling. To pay them, run the local server with a small capped wallet
(<code>npx -y @romudille/agentpay-mcp</code> with <code>AGENTPAY_ENABLE_PAID=1</code> and
<code>AGENTPAY_MAX_SPEND</code>) in Claude Desktop or Claude Code, or use the
<code>agentpay-x402</code> Python SDK. Details: <a href="https://www.npmjs.com/package/@romudille/agentpay-mcp">npm README</a>.</p>
"""


def render_connect_page(slug: str, gateway_url: str) -> str:
    h = HARNESSES[slug]
    title = f"Add AgentPay to {h['name']} — remote MCP connector"
    desc = f"Connect {gateway_url}/mcp as a custom connector in {h['name']}: {h['blurb']}"
    steps = "\n".join(f"<li>{s}</li>" for s in h["steps"])
    notes = "".join(f'<p class="note">{n}</p>' for n in h["notes"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(desc)}">
<link rel="canonical" href="{gateway_url}/connect/{_e(slug)}">
<meta property="og:site_name" content="AgentPay">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(desc)}">
<meta property="og:type" content="article">
<meta property="og:url" content="{gateway_url}/connect/{_e(slug)}">
<meta property="og:image" content="{gateway_url}/og.png">
<link rel="icon" type="image/svg+xml" href="{gateway_url}/favicon.svg">
<script type="application/ld+json">{_json_ld(slug, h, gateway_url)}</script>
<style>{_CSS}{_EXTRA_CSS}</style></head><body><div class="wrap">
<p class="crumb"><a href="{gateway_url}/connect">← Connect AgentPay</a></p>
<h1>Add AgentPay to {_e(h["name"])}</h1>
<p class="meta">Steps checked against the vendor's docs on {VERIFIED} · {_e(h["plans"])}</p>
{_tabs(gateway_url, slug)}
<h2>The URL</h2>
<code class="url">{gateway_url}/mcp</code>
<h2>Steps</h2>
<ol>{steps}</ol>
{notes}
{_common(gateway_url)}
<p class="meta">Source for the steps: <a href="{_e(h["source"])}">{_e(h["source"])}</a></p>
{_foot(gateway_url)}
</div></body></html>"""


def render_connect_index(gateway_url: str) -> str:
    title = "Connect AgentPay — remote MCP for claude.ai, ChatGPT, Perplexity and Grok"
    desc = (f"One URL, {gateway_url}/mcp, installs AgentPay's keyless tool set as a custom "
            "connector in claude.ai, ChatGPT, Perplexity and Grok.")
    items = "\n".join(
        f'<li><a href="{gateway_url}/connect/{_e(s)}"><strong>{_e(h["name"])}</strong></a>'
        f'<br><span class="mut">{_e(h["blurb"])}</span></li>'
        for s, h in HARNESSES.items())
    json_ld = json.dumps({
        "@context": "https://schema.org", "@type": "CollectionPage",
        "name": "Connect AgentPay", "description": desc, "url": f"{gateway_url}/connect",
    }, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(desc)}">
<link rel="canonical" href="{gateway_url}/connect">
<meta property="og:site_name" content="AgentPay">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{gateway_url}/connect">
<meta property="og:image" content="{gateway_url}/og.png">
<link rel="icon" type="image/svg+xml" href="{gateway_url}/favicon.svg">
<script type="application/ld+json">{json_ld}</script>
<style>{_CSS}{_EXTRA_CSS}</style></head><body><div class="wrap">
<p class="crumb"><a href="{gateway_url}/">← AgentPay</a></p>
<h1>Connect AgentPay</h1>
<p class="mut">Remote MCP, Streamable HTTP, no auth. Paste one URL into the harness you use.</p>
<code class="url">{gateway_url}/mcp</code>
<ul>{items}</ul>
{_common(gateway_url)}
<h2>Local, with a wallet</h2>
<p>Claude Desktop / Claude Code: <code>npx -y @romudille/agentpay-mcp</code> (stdio). Keyless by
default; set <code>AGENTPAY_ENABLE_PAID=1</code> after funding the wallet it mints, or bring a key with
<code>AGENTPAY_BASE_KEY</code>, to settle paid tools under <code>AGENTPAY_MAX_SPEND</code>.</p>
{_foot(gateway_url)}
</div></body></html>"""
