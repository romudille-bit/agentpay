-- service_probes + service_scores — the Active Prober's delivery telemetry
-- (PROBER_SPEC 2026-07-07 / AGE-6).
--
--   service_probes  — one row per probe (T0 free or T1 paid). Raw evidence,
--                     PRIVATE: tx_hash + error snapshots back every negative
--                     public claim (defamation defense for
--                     took_payment_no_delivery), so no public read policy.
--   service_scores  — one row per service, rebuilt from the 30d window on
--                     every prober ingest (POST /v1/prober/run). PUBLIC
--                     SELECT: the scores are the public asset.
--
-- Apply once in the Supabase SQL Editor:
--   https://supabase.com/dashboard/project/<project-ref>/sql
-- (Same convention as flagship_runs.sql — the gateway reads/writes with its
--  secret key, which bypasses RLS; the policies below only govern anon reads.)

CREATE TABLE IF NOT EXISTS service_probes (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    probed_at         timestamptz NOT NULL DEFAULT now(),
    resource_url      text NOT NULL,
    pay_to            text,
    network           text,
    price_usdc        numeric,
    probe_type        text NOT NULL,            -- 'free' | 'paid'
    alive             boolean,
    x402_wellformed   boolean,
    price_matches     boolean,
    mpp_option        boolean,                  -- [MR-3] MPP/Tempo advertised (detection only)
    usdg_option       boolean,                  -- AGE-18: USDG/Robinhood Chain advertised (detection only)
    settle_ok         boolean,
    http_ok           boolean,
    latency_ms        integer,
    response_nonempty boolean,
    schema_ok         boolean,
    tx_hash           text,
    error             text
);

CREATE INDEX IF NOT EXISTS idx_service_probes_url_at
    ON service_probes (resource_url, probed_at DESC);
CREATE INDEX IF NOT EXISTS idx_service_probes_at
    ON service_probes (probed_at DESC);

CREATE TABLE IF NOT EXISTS service_scores (
    resource_url    text PRIMARY KEY,
    window_days     integer NOT NULL DEFAULT 30,
    paid_probes     integer,
    delivery_rate   numeric,
    delivery_factor numeric,
    latency_p50_ms  integer,
    last_ok_at      timestamptz,
    last_fail_at    timestamptz,
    flags           jsonb DEFAULT '[]'::jsonb,
    mpp_option      boolean DEFAULT false,   -- [MR-3] MPP/Tempo advertised (AGE-8)
    usdg_option     boolean DEFAULT false,   -- USDG/Robinhood Chain advertised (AGE-18)
    price_usdc      numeric,                 -- last-known advertised price (AGE-8)
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- AGE-8/AGE-18/AGE-20 addenda — idempotent for tables created before these columns.
ALTER TABLE service_probes ADD COLUMN IF NOT EXISTS usdg_option boolean;
ALTER TABLE service_probes ADD COLUMN IF NOT EXISTS name text;   -- Bazaar serviceName
ALTER TABLE service_probes ADD COLUMN IF NOT EXISTS need text;   -- discovery category
ALTER TABLE service_scores ADD COLUMN IF NOT EXISTS mpp_option boolean DEFAULT false;
ALTER TABLE service_scores ADD COLUMN IF NOT EXISTS usdg_option boolean DEFAULT false;
ALTER TABLE service_scores ADD COLUMN IF NOT EXISTS price_usdc numeric;
ALTER TABLE service_scores ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE service_scores ADD COLUMN IF NOT EXISTS need text;
ALTER TABLE service_scores ADD COLUMN IF NOT EXISTS network text;  -- settlement chain (CAIP-2)

-- AGE-83: a delivery verdict resting on ONE paid probe is not a verdict.
--   confidence         — 'provisional' (1 paid probe) | 'confirmed' (>=2) | NULL
--   no_delivery_probes — paid probes that settled and delivered nothing; the
--                        count the took_payment_no_delivery flag is built from
-- The gateway degrades gracefully when this block hasn't been applied yet (see
-- _SCORE_COLUMNS_OPTIONAL in gateway/services/supabase.py): scores keep being
-- written and read, minus these two fields. The re-probe queue is NOT affected —
-- retest_queue() keys off paid_probes + delivery_rate, both of which predate
-- AGE-83. What is lost without this block is the public confidence tier on
-- /scores.json and /probes: a verdict resting on one probe stops being labelled
-- as one, which is the whole point of the field.
ALTER TABLE service_scores ADD COLUMN IF NOT EXISTS confidence text;
ALTER TABLE service_scores ADD COLUMN IF NOT EXISTS no_delivery_probes integer;

-- RLS: raw probes private, scores public-read (the gateway's secret key
-- bypasses RLS for all reads/writes either way).
ALTER TABLE service_probes ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_scores ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS service_scores_public_read ON service_scores;
CREATE POLICY service_scores_public_read
    ON service_scores FOR SELECT
    USING (true);
