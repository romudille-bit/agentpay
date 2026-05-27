"""
gateway/landing.py — HTML landing page served at https://agentpay.tools/

Served by gateway/routes/infra.py when GET / sees Accept: text/html in the
request. Agents and API clients hitting the same URL with Accept: application/json
(or no Accept) get the JSON manifest as before.

Single file, no external assets, no JS, no frameworks. CSS is embedded.
Dark theme. Renders cleanly on mobile down to ~360px viewport.

To preview locally:
    uvicorn gateway.main:app --port 8001 --reload
    open http://localhost:8001
"""

from registry import Tool


_LANDING_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AgentPay — economic intelligence for autonomous agents</title>
<meta name="description" content="17 free tools to start, full session visibility, metered inference coming. AgentPay gives autonomous agents economic intelligence — a wallet, a budget cap, and the awareness to spend it well.">
<meta property="og:title" content="AgentPay — economic intelligence for autonomous agents">
<meta property="og:description" content="Your agent shouldn't just have a budget. It should know how to use one. 17 free tools, session receipts on every call, metered inference coming.">
<meta property="og:url" content="GATEWAY_URL_PLACEHOLDER">
<meta property="og:type" content="website">
<link rel="canonical" href="GATEWAY_URL_PLACEHOLDER">
<style>
:root {
  --bg: #0a0a0b;
  --fg: #e8e8e8;
  --muted: #8a8a92;
  --accent: #5eead4;
  --price: #4ade80;
  --code-bg: #131316;
  --border: #1f1f24;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 16px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 0.88em; }

nav {
  max-width: 960px;
  margin: 0 auto;
  padding: 1.5rem 2rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
}
nav .logo { font-weight: 600; font-size: 1.1rem; letter-spacing: -0.01em; }
nav ul { list-style: none; padding: 0; margin: 0; display: flex; gap: 1.5rem; }
nav a { color: var(--muted); font-size: 0.9rem; }
nav a:hover { color: var(--fg); text-decoration: none; }

main { max-width: 960px; margin: 0 auto; padding: 0 2rem; }

.hero { padding: 4rem 0 3rem; }
.hero h1 {
  font-size: 2.6rem;
  font-weight: 600;
  margin: 0 0 1.25rem;
  letter-spacing: -0.025em;
  line-height: 1.1;
}
.hero .subtitle {
  color: var(--muted);
  font-size: 1.15rem;
  max-width: 640px;
  margin: 0 0 2rem;
}
.cta {
  display: inline-block;
  padding: 0.7rem 1.4rem;
  background: var(--accent);
  color: #0a0a0b;
  font-weight: 600;
  border-radius: 6px;
  margin-right: 0.5rem;
  font-size: 0.95rem;
}
.cta:hover { text-decoration: none; opacity: 0.88; }
.cta.secondary {
  background: transparent;
  color: var(--accent);
  border: 1px solid var(--accent);
  padding: calc(0.7rem - 1px) calc(1.4rem - 1px);
}

section { padding: 2.5rem 0; border-top: 1px solid var(--border); }
section h2 {
  font-size: 0.82rem;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 1.5rem;
}

pre {
  background: var(--code-bg);
  padding: 1.25rem 1.5rem;
  border-radius: 8px;
  overflow-x: auto;
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85rem;
  line-height: 1.65;
  margin: 0;
  border: 1px solid var(--border);
}
.snippet-note { color: var(--muted); margin: 1rem 0 0; font-size: 0.9rem; }
.snippet-note code { background: var(--code-bg); padding: 0.1rem 0.4rem; border-radius: 3px; }

.tools-list { list-style: none; padding: 0; margin: 0; }
.tools-list li {
  display: grid;
  grid-template-columns: 180px 80px 1fr;
  gap: 1.5rem;
  padding: 0.65rem 0;
  border-bottom: 1px solid var(--border);
  align-items: baseline;
  font-size: 0.9rem;
}
.tools-list li:last-child { border-bottom: none; }
.tool-name { font-family: "SF Mono", Menlo, Consolas, monospace; color: var(--fg); }
.tool-price { color: var(--price); font-family: "SF Mono", Menlo, Consolas, monospace; }
.tool-desc { color: var(--muted); }

.how-steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1.75rem; }
.how-step .num {
  color: var(--accent);
  font-family: "SF Mono", monospace;
  font-size: 0.78rem;
  letter-spacing: 0.05em;
}
.how-step h3 { font-size: 1rem; font-weight: 600; margin: 0.4rem 0 0.5rem; }
.how-step p { color: var(--muted); font-size: 0.9rem; margin: 0; }

.networks-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.network-card {
  padding: 1.25rem 1.5rem;
  background: var(--code-bg);
  border-radius: 8px;
  border: 1px solid var(--border);
}
.network-card h3 { font-size: 1rem; font-weight: 600; margin: 0 0 0.5rem; }
.network-card p { color: var(--muted); font-size: 0.88rem; margin: 0; line-height: 1.55; }

footer {
  max-width: 960px;
  margin: 0 auto;
  padding: 2.5rem 2rem;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 0.85rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 1.5rem;
}
footer .alignment { max-width: 540px; }
footer ul { list-style: none; padding: 0; margin: 0; display: flex; gap: 1.25rem; }

@media (max-width: 640px) {
  .hero h1 { font-size: 1.8rem; }
  .hero .subtitle { font-size: 1.05rem; }
  .tools-list li { grid-template-columns: 1fr 80px; }
  .tool-desc { grid-column: 1 / -1; padding-top: 0.2rem; }
  .how-steps, .networks-grid { grid-template-columns: 1fr; }
  nav { flex-direction: column; align-items: flex-start; gap: 0.75rem; padding: 1.5rem; }
  main { padding: 0 1.5rem; }
  .cta { display: block; text-align: center; margin: 0 0 0.5rem; }
  footer { flex-direction: column; align-items: flex-start; padding: 2.5rem 1.5rem; }
}
</style>
</head>
<body>

<nav>
  <div class="logo">AgentPay</div>
  <ul>
    <li><a href="#snippet">Quick start</a></li>
    <li><a href="#tools">Tools</a></li>
    <li><a href="https://github.com/romudille-bit/agentpay">GitHub</a></li>
  </ul>
</nav>

<main>

<section class="hero">
  <h1>Your agent shouldn't just have a budget. It should know how to use one.</h1>
  <p class="subtitle">AgentPay gives autonomous agents economic intelligence — 17 free tools to start, full visibility for you, and metered inference when you're ready.</p>
  <a href="#snippet" class="cta">Start free — 17 tools, zero cost →</a>
  <a href="#tools" class="cta secondary">Browse the tools</a>
  <p class="snippet-note" style="margin-top:1rem;">17 tools · All free · Session receipts on every call · Works with LangChain, CrewAI, any Python agent</p>
</section>

<section id="snippet" class="snippet">
  <h2>5 lines. 17 tools. Zero cost.</h2>
<pre><code># pip install agentpay-x402

from agentpay import AgentWallet, Session

wallet = AgentWallet(network="mainnet")   # or testnet

# All 17 tools — zero cost, session receipts on every call
with Session(wallet, gateway_url="GATEWAY_URL_PLACEHOLDER") as session:
    page    = session.call("url_reader",      {"url": "https://example.com"})
    results = session.call("web_search",      {"query": "ETH gas fees today"})
    market  = session.call("market_snapshot", {})
    whales  = session.call("whale_activity",  {"token": "ETH"})
    news    = session.call("crypto_news",     {"currencies": "ETH,BTC"})

    print(session.spending_summary())
    # { "calls": 5, "spent": "$0", "remaining": "$0.1", "tools": [...] }</code></pre>
  <p class="snippet-note">All 17 tools are free. Every call gets a session receipt — every tool called, every cost, every timestamp. Works with LangChain, CrewAI, AutoGen, or plain Python. No USDC needed to start.</p>
</section>

<section id="tools" class="tools">
  <h2>17 tools — all free</h2>
  <ul class="tools-list">
TOOLS_ROWS_PLACEHOLDER
  </ul>
</section>

<section class="how">
  <h2>How it works</h2>
  <div class="how-steps">
    <div class="how-step">
      <span class="num">01</span>
      <h3>Agent calls a tool</h3>
      <p>POST to <code>/tools/&lt;name&gt;/call</code> with parameters.</p>
    </div>
    <div class="how-step">
      <span class="num">02</span>
      <h3>Gateway returns 402</h3>
      <p>HTTP 402 with payment instructions: amount, address, memo. Pure x402 protocol.</p>
    </div>
    <div class="how-step">
      <span class="num">03</span>
      <h3>Pay on-chain, retry</h3>
      <p>Agent sends USDC, retries with <code>X-Payment</code> header. Data returned, payment verified on-chain.</p>
    </div>
  </div>
</section>

<section class="networks">
  <h2>Settlement layer</h2>
  <div class="networks-grid">
    <div class="network-card">
      <h3>Stellar mainnet</h3>
      <p>Native USDC. Sub-cent settlement (~$0.000001 per tx). As of May 2026, Circle's CCTP is live on Stellar, so agents can fund from any of 23 supported chains and settle here. Ready for paid tools when metered inference ships.</p>
    </div>
    <div class="network-card">
      <h3>Base mainnet</h3>
      <p>Native USDC. Discovery via Coinbase's Bazaar directory, auto-indexed through the CDP facilitator. Discovery on Base, settlement on Stellar — the dual-network strategy.</p>
    </div>
  </div>
</section>

</main>

<footer>
  <div class="alignment">
    AgentPay implements x402-v2 paywall headers with direct Horizon-verified settlement — aligned with the
    <a href="https://developers.stellar.org/docs/build/agentic-payments/x402">Stellar Foundation's agentic payments roadmap</a>.
    Compatible with MPP-Charge-style per-request settlement; no facilitator dependency required.
  </div>
  <ul>
    <li><a href="https://github.com/romudille-bit/agentpay">GitHub</a></li>
    <li><a href="https://glama.ai/mcp/servers/romudille-bit/agentpay">MCP</a></li>
    <li><a href="GATEWAY_URL_PLACEHOLDER/.well-known/agentpay.json">Manifest</a></li>
  </ul>
</footer>

</body>
</html>"""


def render_landing(tools: list[Tool], gateway_url: str) -> str:
    """Build the landing page HTML from the live tool registry.

    Tools are rendered as a simple grid (name, price, description), sorted by
    price ascending so the cheapest entry-point tools appear first.
    """
    def _price_label(price_usdc: str) -> str:
        try:
            return "Free" if float(price_usdc) == 0 else f"${price_usdc}"
        except (ValueError, TypeError):
            return f"${price_usdc}"

    tools_rows = "\n".join(
        f'    <li>'
        f'<span class="tool-name">{t.name}</span>'
        f'<span class="tool-price">{_price_label(t.price_usdc)}</span>'
        f'<span class="tool-desc">{_escape(t.description)}</span>'
        f'</li>'
        for t in sorted(tools, key=lambda x: x.name)
        if t.active
    )
    return (
        _LANDING_TEMPLATE
        .replace("GATEWAY_URL_PLACEHOLDER", gateway_url)
        .replace("TOOLS_ROWS_PLACEHOLDER", tools_rows)
    )


def _escape(s: str) -> str:
    """Minimal HTML escape for tool descriptions. Don't trust registry text
    blindly — descriptions come from a Python dict today but in the future
    may be sourced from Supabase, so escape defensively."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )
