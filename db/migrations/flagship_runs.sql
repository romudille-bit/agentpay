-- flagship_runs — one row per flagship analyst run.
-- Feeds the reasoning shown on GET /ledger (plan estimate, regime read,
-- per-verdict factor breakdown, spend receipt). Written by the gateway on
-- POST /v1/flagship/run; read by GET /ledger.json.
--
-- Apply once in the Supabase SQL Editor:
--   https://supabase.com/dashboard/project/<project-ref>/sql
-- (The REST /rest/v1/sql DDL path needs a service_role key; this file is the
--  manual fallback. The gateway reads/writes with its secret key, which bypasses
--  RLS, so no public policy is required for /ledger to work.)

CREATE TABLE IF NOT EXISTS flagship_runs (
    id          SERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL,
    wallet      TEXT,
    max_spend   TEXT,
    objective   JSONB       DEFAULT '{}'::jsonb,
    plan        JSONB       DEFAULT '{}'::jsonb,
    regime      TEXT        DEFAULT '',
    context     TEXT        DEFAULT '',
    verdicts    JSONB       DEFAULT '{}'::jsonb,
    skipped     JSONB       DEFAULT '{}'::jsonb,
    receipt     JSONB       DEFAULT '{}'::jsonb,
    free_intel  JSONB       DEFAULT '{}'::jsonb,
    note        TEXT        DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_flagship_runs_run_at ON flagship_runs (run_at DESC);
