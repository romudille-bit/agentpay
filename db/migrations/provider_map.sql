-- provider_map + provider_depth — AGE-138 (2026-08-27)
--
-- The map is ours or it is nothing: every sweep resolves payTo → provider
-- (names, hosts, URLs, needs) and every verified_route call scores ~200
-- candidates — then keeps only the pick. And the market's dollars concentrate
-- OFF-catalog (the largest recipient in the 08-24 forensics was not among
-- Bazaar's listings), so a Bazaar-derived list can never be the map. Two
-- tables, both BATCH-written (never from the live 402 path — the 08-20 disk-IO
-- fix is the rule: one upsert per sweep, not per request):
--
--   provider_map    — the ENTITY. One row per (pay_to, network): which hosts /
--                     resource URLs / needs this wallet is paid for, where we
--                     saw it (sweep, prober, estimate_plan, bazaar, chain…),
--                     a denormalised copy of its last delivery evidence, and
--                     the self-claim columns (empty today, in the schema from
--                     day one so provider claim lands without a migration).
--                     Written by the prober ingest (once per sweep) and by
--                     the daily rollup flush (verified_route discoveries held
--                     in memory until then). PUBLIC read: payTo + host are
--                     what Bazaar already publishes.
--
--   provider_depth  — the ON-CHAIN payer shape per seller, 30d window, from
--                     tools/sql/payer_depth_x402.sql (Dune; run from a laptop
--                     by tools/payer_depth.py). Aggregates ONLY — no payer
--                     wallets are stored (those stay in Dune). This is what
--                     turns "23 unique payers" into "23 payers, 11 weighted
--                     after the prober discount, 4 came back": AGE-133 phase 0.
--                     PUBLIC read: the numbers appear in verified_route's why.
--
-- Apply once in the Supabase SQL Editor (same convention as service_probes.sql;
-- the gateway writes with its secret key, the policies govern anon reads).

CREATE TABLE IF NOT EXISTS provider_map (
    pay_to          text        NOT NULL,           -- lowercased
    network         text        NOT NULL,           -- CAIP-2 (eip155:8453, solana:…)
    host            text,                           -- primary host (most listings)
    display_name    text,                           -- best serviceName / host
    resource_urls   jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- ["https://…", …] (capped)
    categories      jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- {"web search": sweeps_seen, …}
    sources         text[]      NOT NULL DEFAULT '{}',          -- sweep|prober|estimate_plan|bazaar|chain|claimed
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    listings        integer,                        -- distinct resource URLs seen under this payTo
    evidence        jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- {delivery_rate, paid_probes, payers30d, calls30d, flags, scored_urls}
    claimed_by      text,                           -- provider self-claim (empty until the claim flow ships)
    claim_proof     jsonb,
    PRIMARY KEY (pay_to, network)
);

CREATE INDEX IF NOT EXISTS idx_provider_map_host ON provider_map (host);
CREATE INDEX IF NOT EXISTS idx_provider_map_last_seen ON provider_map (last_seen DESC);

CREATE TABLE IF NOT EXISTS provider_depth (
    pay_to            text        NOT NULL,         -- lowercased
    network           text        NOT NULL,
    window_days       integer     NOT NULL DEFAULT 30,
    payers            integer,                      -- distinct payers, on-chain
    legs              integer,                      -- settled legs, on-chain
    usd               numeric,
    mean_leg          numeric,
    returning_payers  integer,                      -- payers with >= 2 legs to THIS payTo
    retention         numeric,                      -- returning_payers / payers
    effective_payers  numeric,                      -- Σ payer_weight (1.0 returning; 0.2..1.0 one-leg by fanout)
    payer_quality     numeric,                      -- effective_payers / payers
    prober_share      numeric,                      -- payers weighted < 0.5 / payers
    p50_legs_per_payer numeric,                     -- fleet shape: median legs per payer
    top_payer_share   numeric,                      -- fleet shape: largest payer's share of usd
    first_leg_at      timestamptz,
    last_leg_at       timestamptz,
    source            text        NOT NULL DEFAULT 'dune',
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pay_to, network)
);

CREATE INDEX IF NOT EXISTS idx_provider_depth_updated ON provider_depth (updated_at DESC);

ALTER TABLE provider_map   ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_depth ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS provider_map_public_read ON provider_map;
CREATE POLICY provider_map_public_read
    ON provider_map FOR SELECT
    USING (true);

DROP POLICY IF EXISTS provider_depth_public_read ON provider_depth;
CREATE POLICY provider_depth_public_read
    ON provider_depth FOR SELECT
    USING (true);
