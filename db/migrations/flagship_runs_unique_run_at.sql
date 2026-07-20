-- flagship_runs: make run_at unique — atomic ingest idempotency.
-- Follow-up review 2026-07-20 (remaining low): POST /v1/flagship/run's
-- duplicate check is check-then-insert, not atomic — two concurrent
-- duplicate POSTs could both pass the existence check and double-insert.
-- run_at is already the documented idempotency key; enforcing it in the
-- database closes the race for free. After this index, the loser of a
-- concurrent duplicate insert gets a PostgREST 409 (unique violation),
-- which the ingest path logs and the caller can safely ignore/retry.
--
-- Apply once in the Supabase SQL Editor:
--   https://supabase.com/dashboard/project/<project-ref>/sql
--
-- NOTE: if historical duplicates exist, deduplicate first or CREATE UNIQUE
-- INDEX will fail. The DELETE below keeps the lowest id per run_at.

-- 1) One-time dedup (no-op if there are no duplicates):
DELETE FROM flagship_runs a
USING flagship_runs b
WHERE a.run_at = b.run_at AND a.id > b.id;

-- 2) Enforce uniqueness going forward:
CREATE UNIQUE INDEX IF NOT EXISTS uq_flagship_runs_run_at
    ON flagship_runs (run_at);
