-- tools.endpoint: forbid NULL and '' at the database level.
--
-- Why (AGE-107 / AGE-110, 2026-08-06): three tools shipped `endpoint: ""` in
-- GET /tools for weeks. An external audit agent (Circadian) found it, not us.
-- Root cause was two-layered:
--   1) code — the Supabase->seed hydration fallback in gateway/main.py repaired
--      four discovery fields but not `endpoint` (fixed in 99f4dd2, which also
--      added a [DISCOVERY-CONTRACT] boot warning + regression tests), and
--   2) data — five rows in `tools` were inserted partially and never backfilled
--      (gas_tracker, open_interest, orderbook_depth, funding_rates,
--      yield_scanner). Backfilled from the seed registry on 2026-08-06.
--
-- Neither code path can create a partial row: POST /tools/register rejects an
-- empty endpoint ("invalid endpoint: required") and persist_tool_registration
-- writes a full asdict(Tool). The partial rows can only have come from manual
-- SQL — which is exactly what application-level guards cannot prevent. Hence a
-- constraint: the database is the only layer every writer must pass through.
--
-- `endpoint` specifically, because it is the one discovery field a buyer cannot
-- reconstruct: session_create is served at /v1/session/create, not
-- /tools/<name>/call, so a client CANNOT assume the path convention — it has to
-- read the field, and a blank one makes the tool unaddressable.
--
-- Apply once in the Supabase SQL Editor:
--   https://supabase.com/dashboard/project/<project-ref>/sql

-- 0) PREFLIGHT — must return 0 rows. If it does not, backfill from
--    registry/registry.py FIRST; the ALTER below will fail otherwise.
SELECT name, endpoint
FROM tools
WHERE endpoint IS NULL OR endpoint = '';

-- 1) No NULLs.
ALTER TABLE tools
    ALTER COLUMN endpoint SET NOT NULL;

-- 2) No empty strings. Idempotent: skips if the constraint already exists.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'tools_endpoint_not_blank'
    ) THEN
        ALTER TABLE tools
            ADD CONSTRAINT tools_endpoint_not_blank CHECK (endpoint <> '');
    END IF;
END $$;

-- 3) VERIFY — expect is_nullable = 'NO' and one row for the check constraint.
SELECT is_nullable
FROM information_schema.columns
WHERE table_name = 'tools' AND column_name = 'endpoint';

SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conname = 'tools_endpoint_not_blank';

-- Rollback, if this ever blocks a legitimate write:
--   ALTER TABLE tools DROP CONSTRAINT tools_endpoint_not_blank;
--   ALTER TABLE tools ALTER COLUMN endpoint DROP NOT NULL;
--
-- Deliberately NOT constrained: description / use_when / returns / triggers.
-- They were never blank in practice, an over-tight constraint would reject
-- legitimate partial registrations, and the boot-time [DISCOVERY-CONTRACT]
-- warning already names any tool serving them empty.

-- ─────────────────────────────────────────────────────────────────────────────
-- STATUS: APPLIED to production 2026-08-09. Verified from outside the SQL
-- editor via PostgREST + the service key, non-destructively — POST a row whose
-- `name` duplicates an existing one, since Postgres evaluates CHECK/NOT NULL
-- before the unique index, so the constraint error fires if it exists and a 409
-- if it does not, and nothing is written either way:
--
--   endpoint = ''    -> 400 23514  violates check constraint
--                       "tools_endpoint_not_blank"
--   endpoint = null  -> 400 23502  null value in column "endpoint"
--                       violates not-null constraint
--   14 rows before, 14 rows after, 0 blank endpoints.
--
-- FOLLOW-UP (AGE-114): the column still carries `DEFAULT ''` from the original
-- CREATE TABLE, which the CHECK now makes unusable — any INSERT omitting
-- `endpoint` hard-fails. Fail-loud is the behaviour we want, but a default the
-- table always rejects is a schema contradicting itself. Drop it:
--
--   ALTER TABLE tools ALTER COLUMN endpoint DROP DEFAULT;
--
-- db/migrate.py's CREATE TABLE has been updated to match production, so a fresh
-- environment gets NOT NULL + the check and no default from the start.
