# AgentPay — Plan of Action

*May 29, 2026 · Output of the build+strategy session. Aligns to STRATEGY.md Phase 1
(free user acquisition) → Phase 2 (visibility). Bias: ship and test, don't chase competitors.*

---

## Where we are

We're in **Phase 1**. This session shipped several Phase-1 enablers and one correctness
fix that was silently blocking the whole free-tool premise.

### Shipped this session (mapped to the roadmap)

| What | Why it matters | Phase |
|---|---|---|
| **Gateway free-tool fix** (price==0 → run directly, no 402) — *deployed* | Phase 1's core mechanic ("free tools, Session-required, $0") literally didn't work before — the gateway 402'd free tools and clients tried to pay. Now unblocked. | 1 (critical) |
| **Receipt includes free calls** (SDK logs $0 calls) | The visibility layer needs every call to appear, free or paid. | 2 foundation |
| **Off-chain Base payment** (sign EIP-3009, settle on accept) | No more paid-but-undelivered loss; agent pays only on delivery. Trust primitive. | 1/2 |
| **`budget_policy()` + `BudgetDecision`** | Cap source precedence + balance clamp + approval gate, as an SDK primitive. | 2 (policy) |
| **Env-provisioned wallet + balance/gas display + interactive cap** | Autonomous-onboarding UX; surfaces the gas-token trap. | 1 |
| **Discovery hardening + `DEMO_EXTERNAL_URL`** | Cross-provider compose step demos cleanly. | 1 |
| **Working end-to-end demo** | Proof artifact: provision → budget → discover → pay → multi-provider spend → cap bites → unified receipt. | 1 |

Note: the SDK methods STRATEGY.md lists as to-do (`remaining()`, `tool_cost()`,
`suggest_cheaper()`, `spending_summary()`) and the policy params (`allowed_tools`,
`max_per_tool`, `rate_limit`) **already exist**. Phase-1 SDK surface is essentially done.

---

## Decisions from this conversation

1. **Stellar: reframe, don't bury.** Circle CCTP is now live on Stellar — native 1:1
   USDC burn/mint to Base + 22 chains, seconds, no bridge. This removes v2's premise that
   Stellar fragments you from the Base/USDC world. Top-of-funnel goes **chain-agnostic**
   ("USDC on Stellar or Base"), not Base-only. Bonus: Stellar has no gas-token trap (the
   "0 ETH for gas" failure we hit on Base), making it the friendlier agent default.
2. **Don't chase competitors.** Keep POSITIONING.md as reference only. The one durable
   line — *neutral, multi-chain, in-path budget* — is already in STRATEGY.md. Ship instead.
3. **The demo is the asset.** It already proves most of Phase 1's "one test that matters."
   Close the gap (agent-native register) and it becomes the canonical proof + the 30s video.

---

## The gap that defines Phase 1 success

> *Can an agent discover AgentPay, register a wallet, use free tools, and get a receipt —
> with zero human involvement?*

Today the demo uses a pre-provisioned key + manual `session_create`. The missing piece is
**`POST /v1/agent/register`** (wallet in one call, no human form). That's the highest-leverage
build item — it turns the demo from "autonomous-ish" into the literal Phase-1 proof.

---

## Action plan

### Now (this week) — close Phase 1

| # | Action | Notes |
|---|---|---|
| 1 | Build **`/v1/agent/register`** → `{wallet_address, session_token}` in one call | The Phase-1 gate. No form, no human. |
| 2 | Rewire the demo to use register + the 3-call flow (`register → /tools → /call`) | Makes the demo *prove* the test that matters. |
| 3 | Expose **session receipt as structured JSON** via an endpoint | `spending_summary()` already produces it; surface it. Phase-2 seed. |
| 4 | Copy pass: `CLAUDE.md`, `README.md` hero — drop x402-as-lead; go **chain-agnostic** (not Base-only, not Stellar-led) | Reflects CCTP reframe + STRATEGY hooks. |
| 5 | CMO skill: update segment messages to the STRATEGY hooks; redirect "Stripe for agents" | Future drafts inherit it. |

### Next (≤2 weeks) — distribution + visibility

| # | Action | Notes |
|---|---|---|
| 6 | **Record the 30-sec budget demo** (register → free calls → receipt → cap bites) | We now have a working flow to record. The comprehension fix. |
| 7 | **LangChain (or CrewAI) PR** — `AgentPaySession` tool | Single highest-leverage distribution move. |
| 8 | Refresh `/llms.txt` as a machine-first manifest + `/.well-known/agent.json` A2A card | Be discoverable by other agents. |
| 9 | Start the **agent-level visibility view** (spend profile, anomaly flags) | Begins Phase 2. |

### Parallel — the validation gate (supply side)

| # | Action | Notes |
|---|---|---|
| 10 | Put the register-flow demo in front of 3–5 agent builders | "Ship and try the waters." Listen for the trigger moment. Not a positioning task. |

---

## What "done with Phase 1" looks like

- An agent registers + uses free tools + gets a receipt, zero human. (Item 1–2)
- 50+ Sessions/week on free tools; ≥1 fully autonomous; one framework PR merged. (STRATEGY criteria)
- Then: start Phase 2 (visibility) in earnest, inference (Phase 3) after trust is built.

---

*Reference docs: POSITIONING.md (competitor map, non-blocking), STRATEGY.md (the bet, phases).*
