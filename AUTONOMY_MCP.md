# AgentPay — Self-Contained MCP + Autonomous Spending Config

*Design doc, 2026-06-03. The Claude Code plugin (live) serves human-supervised agents. This
covers the unlock for UNATTENDED autonomy: a self-contained MCP any runtime can launch, plus
a pre-funded / capped / auto-pick config so an agent can find→judge→pay without a human.*

## The gap this closes

- The plugin only helps agents that ARE Claude Code / Cowork. A LangChain/CrewAI/custom/
  server-deployed agent has no plugin system — it needs an **MCP server, SDK, or API**.
- Today `npx @romudille/agentpay-mcp` is a wrapper that shells to the repo's Python
  `gateway/mcp_server.py` — broken anywhere but the repo. No self-contained option exists.
- The plugin's pay step asks the agent to `pip install` + supply keys at pay-time. Fine in a
  dev session; wrong for a deployed agent, which needs the wallet wired ONCE and payment inline.

## Key custody — clarified (no keys in AgentPay)

Per the peer-to-peer/no-custody decision: **AgentPay's service and the discovery MCP hold NO
secret keys in production.** Roles:
- **AgentPay (seller of routing):** finds tools and *gets paid for that*. Getting paid =
  RECEIVING = a public `payTo` address only, never a secret key. AgentPay never moves a
  provider's money.
- **Providers:** paid **directly by the agent**, bypassing AgentPay.
- **The signing key** (to pay providers + to pay AgentPay's routing fee) belongs to the
  **operator's agent**, lives in the **operator's own runtime/SDK process**, and is managed by
  them. The AgentPay-hosted MCP/gateway never sees it.

So secret keys appear in exactly three places, none of them AgentPay's service:
1. the operator's agent runtime (signs its own outgoing payments — their key, their process);
2. our test harness (`AGENT_BASE_KEY_TEST`, to prove the loop end-to-end);
3. nowhere in AgentPay's gateway or discovery MCP (receive-only + advise-only = keyless).

Consequence for this design: the **MCP is keyless** (free tools + `route` advice + returns
ready-to-pay details). The **pay step + cap + receipts live in the agent's own SDK layer**
(`agentpay-x402`, running in the operator's process with the operator's key) — or in whatever
x402 payment capability the agent already has.

## Pricing & receiving accounts (AgentPay's own charge)

AgentPay gets paid for its work (opening a governed session / the routing+receipt product).
That charge is **received to AgentPay's gateway addresses — keyless (public address only):**
- **Base** (mainnet, default paid chain): `0xE8B25A72dD6aeF69515452a61AD231C7DF2843b7`
- **Stellar** (mainnet, fallback): `GB7THTEVT2T7CZQ5TFUOIQSI32XCJ7BHWS35OBTAI2V4FNL7BXZZ2GM2`

**Price: $0.01 USDC** (raised from $0.001). Rationale: $0.001 sat well below the Base market —
comparable tools there run $0.005–$0.03, nothing below ~$0.005 — so $0.001 read as suspiciously
cheap and left money on the table. Start at **$0.01** and grow from there with demonstrated value.

> Making $0.01 live is a gateway change, separate from this doc: set `SESSION_PRICE_USDC="0.01"`
> in `gateway/routes/session.py` (atomic `10000`), update `tools/index_bazaar.py` +
> `tools/bazaar_tx.py` `AMOUNT`, the registry's `session_create` price, redeploy the gateway,
> then **re-index Bazaar** so the listed `accepts` amount updates to $0.01.

## Transaction cost / network fees (confirmed)

- **Base (EIP-3009 via CDP facilitator — our default paid path): $0 gas to the agent.** The
  agent signs the authorization *off-chain*; the CDP facilitator submits the tx and pays the gas.
  The agent's only cost is the USDC amount itself. EVM accounts need no native-token reserve to
  exist. This gaslessness is exactly why Base is the default chain.
  - (Caveat: only on the Mode-A/CDP path. A raw Mode-B on-chain transfer would cost ETH gas —
    we don't use it for this.)
- **Stellar: near-zero per tx, but NOT truly free.** Per-payment network fee is ~0.00001 XLM
  (100 stroops) — fractions of a cent, but nonzero, so the agent must hold a trivial XLM balance.
  More notably there's a one-time **reserve**: a Stellar account needs ~1 XLM base reserve to
  exist + ~0.5 XLM for the USDC trustline (~1.5 XLM locked) before it can hold/pay USDC. So
  Stellar carries small XLM overhead that Base does not — another reason Base is default.

## Part A — Self-contained MCP server (keyless)

**Decision: Node-native local MCP (recommended).** Rewrite the MCP server in Node (using
`@modelcontextprotocol/sdk`), bundled fully in the npm package, so `npx @romudille/agentpay-mcp`
runs anywhere Node runs — no Python, no repo, no pip. Most agent runtimes/containers have Node.
- Rejected: Python-bundled (pip fragility we hit twice); remote/hosted HTTP MCP (raises
  where-does-signing-happen → custody; keep for later as a convenience, signing stays client-side).

**Tools the keyless MCP exposes:**
- the 17 **free data tools** — proxied over HTTP to the agentpay.tools gateway (free x402 flow,
  no wallet). This alone fixes the broken npx and restores free tools in the plugin.
- `route(need, budget)` — discover (Bazaar) → decide (junk-filter, usage-rank, budget, price
  tiebreak) → return ranked candidates + recommendation **+ ready-to-pay details** (the chosen
  provider's URL and x402 payment requirements). Keyless.
- `session_status()` — surface the running ledger/receipts the agent has reported (read-only).

The MCP does NOT pay. It returns what's needed to pay; the agent executes the payment itself.

**The pay step lives in the agent's SDK, not the MCP.** The operator's agent uses
`agentpay-x402` (Python) — or any x402 payment capability it has — to settle directly with the
provider, in its own process, with its own key. The SDK does the Base EIP-3009 / Stellar signing,
enforces the budget cap client-side, and produces receipts. (If we later add a Node pay helper it
ships in an SDK/library the operator runs, never in the hosted MCP.)

**MCP config (env) — keyless:**
- `AGENTPAY_GATEWAY_URL` — default https://agentpay.tools.
- (no wallet keys: the MCP never signs.)

**Operator's agent / SDK config (where keys + cap live):**
- `AGENTPAY_BASE_KEY` / `AGENTPAY_STELLAR_SECRET` — the operator's funded wallet (their process).
- `AGENTPAY_MAX_SPEND`, `AGENTPAY_PREFER_CHAIN` — cap + chain, enforced by the SDK at pay time.

## Part B — Autonomous spending config ("pre-funded, capped, auto-pick")

The policy + enforcement lives in the **agent's SDK** (the payer's process), not the keyless
MCP. The MCP advises (route + fits-budget + quality); the SDK enforces the cap and the auto-pick
decision at pay time, where the key is.

**At deploy (once):** operator sets wallet key(s) in the agent's process, FUNDS the wallet, sets
`AGENTPAY_MAX_SPEND`, `AGENTPAY_AUTO_PICK=true`, and the safety limits below. After that the agent
transacts within the policy with no human per call.

**Runtime:** the agent calls the MCP `route(need, budget)` → gets the ranked recommendation +
ready-to-pay details; the SDK (auto-pick on) then pays the top REAL + affordable candidate within
the cap and records the receipt — inline, no human. AgentPay's hosted side never touches the key.

**Safety rails (non-negotiable before auto-pay ships — this is what makes unattended spend
trustworthy, and it's AgentPay's whole reason to exist):**
- **Hard cap** across the session/lifetime (`AGENTPAY_MAX_SPEND`) — can't exceed, period.
- **Per-call max** + **per-period** limit (e.g. max $/hour) to bound runaway loops.
- **Quality floor for auto-pay:** never auto-pay a stub (junk filter) or an unproven provider.
  Require a minimum signal (e.g. `>= N unique payers` in Bazaar `quality`) to auto-pay; below
  it, DON'T auto-pay — degrade to advise (return the list) or escalate if a human channel exists.
  A supervised human can eyeball a bad tool; an unattended agent can't — so the threshold is the
  guardrail.
- **Allowlist / denylist** of providers / payTo addresses.
- **Receipt + telemetry on every payment** — the ledger is the audit trail for unattended spend
  (and feeds the Phase-2b quality moat).

**Decision policy (auto-pick):** auto-pay only if passes junk filter AND quality >= floor AND
price <= min(remaining, per-call cap) AND within per-period limit AND not denylisted. Else:
refuse / degrade to advise / escalate. Never auto-pay on price alone, never below the quality floor.

## Part C — How each runtime consumes it
- **Claude Code / Cowork:** add the self-contained MCP to `plugins/agentpay/.mcp.json` once it
  exists → restores free tools + gives `route`/`pay` as MCP tools alongside the skills.
- **Any MCP runtime (custom/framework agents):** point at `npx @romudille/agentpay-mcp` with env
  config. THIS is the autonomy unlock — AgentPay usable outside Claude Code.
- **Python agents:** the SDK directly (already works).

## Build sequence (each independently shippable)

Keyless MCP (hosted/shareable, no secrets):
1. **MCP-1 — Node free-tools server.** 17 free tools proxied to the gateway, no wallet. Fixes
   broken npx + restores plugin free tools. Smallest, highest immediate value.
2. **MCP-2 — `route` tool** ✅ **DONE (2026-06-04).** (discover+decide, advise) returning ranked
   candidates + ready-to-pay details. No wallet. Shipped in `@romudille/agentpay-mcp@2.1.0`.

Agent-side SDK (operator's process, where keys + cap live):
3. **SDK-pay — settle + cap + receipts.** Base/Stellar signing, client-side budget cap, receipt
   + telemetry. Already largely exists in `agentpay-x402`; harden for the route hand-off.
4. **SDK-autonomy — policy:** auto-pick + quality floor + per-call/per-period limits +
   allow/denylist + `AGENTPAY_AUTO_PICK`. Enforced by the payer's SDK, not the MCP.

## Risks / honest notes
- **Second implementation to keep in sync:** Node signing must match the gateway's x402 format
  and the Python SDK's behavior. Keep the MCP thin; test against the live gateway.
- **Key exposure:** a funded wallet key in an agent's env is a real risk. Mitigate with a
  dedicated low-balance hot wallet funded only to the budget; the hard cap bounds blast radius.
  Recommend operators never put a primary wallet in an agent.
- **Auto-pay is the highest-trust action.** Do NOT ship auto-pick without the quality floor +
  caps + allowlist. Paying real money unattended on a bad signal is the worst failure mode.
- **Remote/hosted MCP** (zero local install) is a future convenience — but signing must stay
  client-side (operator's process), never server-side, or we're back to custody.

## Open decisions
1. **MCP language** — Node-native local (recommended) vs Python-bundled vs remote-hosted.
2. **First ship** — MCP-1 (free tools only) first to fix npx + plugin, then layer route/pay?
   (recommended) or build the full loop before shipping?
3. **Auto-pay quality floor** — what's the default minimum to auto-pay unattended? Proposal:
   require schema + `unique_payers >= 3` (or an explicit per-call override); refuse 0/0 unproven.
4. **Wallet posture guidance** — codify "dedicated hot wallet funded to the cap" as the
   recommended pattern in docs/SDK.
