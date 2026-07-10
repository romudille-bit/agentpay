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
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- RLS: raw probes private, scores public-read (the gateway's secret key
-- bypasses RLS for all reads/writes either way).
ALTER TABLE service_probes ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_scores ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS service_scores_public_read ON service_scores;
CREATE POLICY service_scores_public_read
    ON service_scores FOR SELECT
    USING (true);
