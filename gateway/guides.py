"""
gateway/guides.py — long-form technical guides, server-rendered.

Why these live on the gateway and not on a third-party blog: search authority
and answer-engine citations accrue to the domain that serves the words. Every
guide is one crawlable URL with its own title/description/canonical/OG tags and
schema.org TechArticle JSON-LD, in the same visual system as the tool pages.

Adding a guide: append an entry to GUIDES. Body strings are PLAIN strings (not
f-strings) because they contain JSON braces — `GATEWAY_URL_PLACEHOLDER` is
substituted at render time, same convention as landing.py.
"""
import json

from gateway.tool_pages import _CSS, _e, _foot

# ── Guide 1 — x402 envelope compliance ────────────────────────────────────────
_X402_TRUST_BODY = """
<p class="lead">On 12 August 2026 three of our paid endpoints carried a grade of
<strong>C</strong> on a public x402 trust directory, each with a machine-readable
recommendation attached: <code>avoid</code>. Downtime wasn't the cause. One
missing block in the 402 response body was — and it had failed every probe for
thirty days straight.</p>

<p>This is what the bug is, how to check whether your endpoint has it, and how
the scores that punish it are actually computed. If you operate an x402 endpoint,
the check at the bottom takes about thirty seconds.</p>

<h2>Two copies of the truth</h2>

<p>An x402 <code>402 Payment Required</code> response carries its payment
requirements in two places: the <code>PAYMENT-REQUIRED</code> header (base64 JSON)
and the JSON body. Paying clients generally read the header. Indexers, validators
and trust directories generally read the body — it is cheaper to parse and it
doesn't depend on a header that not every implementation sets.</p>

<p>Our envelope was complete in the header. The body carried <code>error</code>,
<code>x402Version</code>, <code>accepts[]</code> and our legacy
<code>payment_options</code> — but no top-level <code>resource</code> object. So
every validator reading the body reached the same verdict:
<code>missing-resource-info</code>.</p>

<p>The measurement was unambiguous once we could see it: <strong>793 probes over
30 days, zero valid envelopes.</strong> Payments worked the entire time. Agents
paid, tools delivered, receipts settled on-chain. The endpoint was healthy and
publicly labelled untrustworthy.</p>

<h2>How the score is actually computed</h2>

<p>Public directory pages show you a grade. They don't show you which component
produced it, which is why this sat unnoticed: a C looks like a reliability
problem, so you go hunting through uptime and latency. We bought the paid report
instead — half a cent per endpoint, settled over x402 — and it named the
component immediately.</p>

<p>With four observations (two endpoints, before and after the fix) the model
solves exactly:</p>

<pre>score = 0.45 x technicalReliability
      + 0.30 x specCompliance
      + 0.25 x economicReputation

specCompliance = 30 + 70 x (validEnvelopes30d / scoredProbes30d)

grades: A >= 80   B >= 65   C >= 50   D >= 35</pre>

<p>That formula is <em>inferred from observation, not published</em> — treat it
as a working model rather than gospel, and correct us if your own numbers
disagree. But the shape holds, and the shape is the useful part:</p>

<table>
<thead><tr><th>component</th><th>ours, before</th><th>what it measures</th></tr></thead>
<tbody>
<tr><td>technicalReliability</td><td>86</td><td>uptime, weighted with latency</td></tr>
<tr><td><strong>specCompliance</strong></td><td><strong>30</strong></td><td>share of probes with a valid envelope</td></tr>
<tr><td>economicReputation</td><td>47</td><td>settlement history, payer count, age</td></tr>
</tbody>
</table>

<p>An endpoint that was fundamentally healthy — 86 on reliability — sat at 59
overall because one component was pinned at its floor. Compliance is worth 21
points of grade. That is the difference between <code>avoid</code> and an A.</p>

<h2>The part that costs you time: it's a trailing window</h2>

<p><code>specCompliance</code> is a <em>ratio over the trailing 30 days</em>, not
a current-state check. Fixing the bug does not restore the grade; it starts a
thirty-day clock. At roughly 26 probes a day into a 793-probe window, a
corrected envelope earns back about <strong>0.7 points of score per day</strong>.</p>

<p>So the practical cost of shipping this bug is not the day you find it — it is
the month of trailing average you spend climbing out. Every day you leave it
unfixed is a day added to the recovery, and the public page caches on top of
that. Check yours now rather than next quarter.</p>

<h2>Check your own endpoint</h2>

<p>Compare what your 402 says in the body against what it says in the header. If
the header has <code>resource</code> and the body doesn't, you have this bug:</p>

<pre>curl -sD /tmp/h -o /tmp/b https://your-endpoint.example/your/tool

# what validators and directories read:
jq 'keys' /tmp/b

# what paying SDK clients read:
grep -i '^payment-required:' /tmp/h | cut -d' ' -f2 | tr -d '\\r' | base64 -d | jq 'keys'</pre>

<p>Both lists should contain <code>resource</code>. While you're there, confirm
the body's <code>accepts[]</code> entry carries the standard field names
(<code>payTo</code>, <code>maxAmountRequired</code>, <code>asset</code>,
<code>network</code>) — a generic payer that can't find those will silently skip
your endpoint too, and that failure never appears in your logs as an error.</p>

<h2>The fix</h2>

<p>It is additive. Nothing is renamed, nothing is removed, and no existing client
changes behaviour — you are giving body-readers what header-readers already
had:</p>

<pre>{
  "x402Version": 2,
  "error": "Payment required",
  "accepts": [ ... ],
+ "resource": {
+   "url":         "https://your-endpoint.example/your/tool",
+   "serviceName": "What you sell",
+   "description": "One clear sentence",
+   "mimeType":    "application/json"
+ }
}</pre>

<p>The more durable half of the fix is structural: build the envelope <em>once</em>
and emit it to both places, then assert in a test that the body is a superset of
the header. This was our third bug in that family — standard fields reaching only
the header, then one tool serving two different envelopes on two paths, now this.
All three have the same root shape: two copies of the truth, drifting quietly,
with only one of them being watched.</p>

<h2>Did it work?</h2>

<p>Same paid probe, 85 minutes apart, one deploy in between:</p>

<table>
<thead><tr><th></th><th>18:33 UTC</th><th>19:58 UTC</th></tr></thead>
<tbody>
<tr><td>valid envelopes (30d)</td><td>0 / 793</td><td>1 / 793</td></tr>
<tr><td>flags</td><td><code>envelope-noncompliant</code></td><td>cleared</td></tr>
<tr><td>recommendation</td><td><code>avoid</code></td><td><code>caution</code></td></tr>
</tbody>
</table>

<p>The public grade didn't move that day and won't for a while — trailing window,
plus a 24-hour cache on the free page. That is expected, and it is worth writing
down somewhere your future self will find it, because the natural instinct a week
later is to conclude the fix didn't work and go re-diagnose a solved problem.</p>

<h2>Why this is worth an afternoon</h2>

<p>Trust scores are becoming the thing agents route on. An autonomous buyer
choosing between two endpoints that both return data will take the one that
isn't labelled <code>avoid</code> — and unlike a human, it will never read your
documentation to discover that the label was about a JSON field rather than your
service. Every report we bought also named a higher-scoring competitor as a
suggested alternative. That is what a compliance bug actually costs: not a bad
grade, a redirected buyer.</p>

<p>The general rule we'd offer: <strong>your 402 is your storefront.</strong> If
you serve one version of it to SDKs and another to crawlers, you will eventually
be graded on the one you weren't watching.</p>

<p class="note">Credit where it's due: the directory's free page told us we had a
problem, and its paid report told us what the problem was. Half a cent, settled
over x402, to find a bug that thirty days of our own green dashboards had missed.
Buying a probe of yourself is cheap; assuming your own surface is fine is not.</p>

<h2>What we do with this</h2>

<p>AgentPay (<a href="GATEWAY_URL_PLACEHOLDER/">agentpay.tools</a>) runs paid
probes against x402 endpoints — not just liveness checks, but settle-and-verify
runs that confirm a service actually delivers after it takes the money. Those
results are public, with receipts:</p>

<p><a class="cta" href="GATEWAY_URL_PLACEHOLDER/probes">See the delivery scores →</a></p>
"""

GUIDES: dict[str, dict] = {
    "x402-trust-scores": {
        "title": "The x402 envelope bug that quietly tanks your trust score",
        "description": (
            "Our x402 endpoints were graded C and flagged 'avoid' — not for downtime, "
            "but for one missing block in the 402 body. How to check yours in 30 seconds."
        ),
        "blurb": (
            "793 probes, zero valid envelopes, and a public 'avoid' label on a healthy "
            "endpoint. The header/body split that causes it, the scoring model behind "
            "the grade, and a copy-paste check for your own endpoint."
        ),
        "published": "2026-08-13",
        "keywords": "x402 trust score, x402 envelope compliance, 402 payment required, "
                    "x402 endpoint validation, agent payments",
        "body": _X402_TRUST_BODY,
    },
}


def _guide_json_ld(slug: str, g: dict, gateway_url: str) -> str:
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "TechArticle",
        "headline": g["title"],
        "description": g["description"],
        "datePublished": g["published"],
        "dateModified": g["published"],
        "url": f"{gateway_url}/guides/{slug}",
        "mainEntityOfPage": {"@type": "WebPage", "@id": f"{gateway_url}/guides/{slug}"},
        "author": {"@type": "Organization", "name": "AgentPay", "url": gateway_url},
        "publisher": {"@type": "Organization", "name": "AgentPay", "url": gateway_url},
        "keywords": g["keywords"],
        "isAccessibleForFree": True,
    }, separators=(",", ":"))


_EXTRA_CSS = """
.lead{font-size:1.1rem}
.mut{color:var(--mut)}
.note{border-left:3px solid var(--line);padding-left:14px;color:var(--mut)}
.cta{display:inline-block;margin:8px 0 24px;padding:10px 16px;border:1px solid var(--ac);
     border-radius:8px;color:var(--ac);text-decoration:none}
.meta{color:var(--mut);font-size:.9rem;margin-top:-8px}
pre{white-space:pre-wrap}
"""


def render_guide(slug: str, gateway_url: str) -> str:
    """One indexable page per guide."""
    g = GUIDES[slug]
    body = g["body"].replace("GATEWAY_URL_PLACEHOLDER", gateway_url)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(g["title"])}</title>
<meta name="description" content="{_e(g["description"])}">
<link rel="canonical" href="{gateway_url}/guides/{_e(slug)}">
<meta property="og:site_name" content="AgentPay">
<meta property="og:title" content="{_e(g["title"])}">
<meta property="og:description" content="{_e(g["description"])}">
<meta property="og:type" content="article">
<meta property="og:url" content="{gateway_url}/guides/{_e(slug)}">
<meta property="og:image" content="{gateway_url}/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="{gateway_url}/favicon.svg">
<script type="application/ld+json">{_guide_json_ld(slug, g, gateway_url)}</script>
<style>{_CSS}{_EXTRA_CSS}</style></head><body><div class="wrap">
<p class="crumb"><a href="{gateway_url}/guides">← AgentPay guides</a></p>
<h1>{_e(g["title"])}</h1>
<p class="meta">Published {_e(g["published"])} · AgentPay (agentpay.tools)</p>
{body}
{_foot(gateway_url)}
</div></body></html>"""


def render_guides_index(gateway_url: str) -> str:
    """Indexable hub linking every guide."""
    title = "Guides — x402, agent payments, and spend control | AgentPay"
    desc = ("Practical guides for x402 endpoint operators and agent builders: envelope "
            "compliance, trust scores, marketplace indexing, and budget-capped agent spend.")
    items = "\n".join(
        f'<li><a href="{gateway_url}/guides/{_e(s)}"><strong>{_e(g["title"])}</strong></a>'
        f'<br><span class="mut">{_e(g["blurb"])}</span>'
        f'<br><span class="meta">{_e(g["published"])}</span></li>'
        for s, g in sorted(GUIDES.items(), key=lambda kv: kv[1]["published"], reverse=True)
    )
    json_ld = json.dumps({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "AgentPay guides",
        "description": desc,
        "url": f"{gateway_url}/guides",
    }, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(desc)}">
<link rel="canonical" href="{gateway_url}/guides">
<meta property="og:site_name" content="AgentPay">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{gateway_url}/guides">
<meta property="og:image" content="{gateway_url}/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="{gateway_url}/favicon.svg">
<script type="application/ld+json">{json_ld}</script>
<style>{_CSS}{_EXTRA_CSS}
ul{{list-style:none;padding:0}} li{{margin:0 0 22px}}</style></head><body><div class="wrap">
<h1>Guides</h1>
<p class="lead">Field notes from running an x402 gateway — written for the people
operating endpoints and the people building agents that pay them.</p>
<ul>
{items}
</ul>
{_foot(gateway_url)}
</div></body></html>"""
