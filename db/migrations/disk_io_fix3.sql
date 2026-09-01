-- Disk-IO fix #3 — 2026-09-01 (3rd "Disk IO Budget" email, 2026-08-29)
--
-- Fix #2 (2f0b03b, 08-20) held: payment_logs gets ~10 real rows/day now.
-- What kept draining the Nano budget afterwards was the OTHER side of that
-- fix plus the legacy it left behind:
--
--   1. payment_logs_daily_rollup grew ~8,400 rows/day (139,848 rows by
--      09-01): rows are additive events and the 5-min flush appended one
--      row per live (day, tool, UA, kind) key — 288 flushes × ~30 keys.
--      CarbonMonitor alone was 288 rows/day per tool. The gateway now
--      flushes hourly (gateway/services/probe_rollup.py); this file
--      compacts what is already there (~140k → ~one row per key per day).
--
--   2. payment_logs still carries 62,794 'abandoned' rows (61,105 of them
--      pre-fix-#2 bot 402s) and has NO index on `state`. Two background
--      readers full-scanned it: the abandoned sweep (every 5 min, per
--      gateway, always matching 0 rows since fix #2) and /scores.json's
--      receipts query (per crawler hit). Both are throttled in code. The
--      dashboard shows 100% cache hit, so these cost CPU more than disk —
--      the disk cost of this table is its 77 MB of bloat (block B).
--
-- Apply once in the Supabase SQL Editor (project twdtvssqfpgydsvwqglt):
--   https://supabase.com/dashboard/project/twdtvssqfpgydsvwqglt/sql
-- Run blocks A → (C) → D one at a time, top to bottom (B is a note). A is idempotent.
-- Block C is OPTIONAL and irreversible — read its note first.

-- ── A) Compact the rollup: one row per (day, tool, UA, state, network) ──
-- Consumers SUM(n) GROUP BY the same key, so totals are identical before
-- and after. Runs in one transaction: either the whole table is compacted
-- or nothing changes.
BEGIN;
CREATE TEMP TABLE rollup_compact AS
    SELECT day, tool_name, user_agent, state, network, SUM(n)::bigint AS n
    FROM payment_logs_daily_rollup
    GROUP BY day, tool_name, user_agent, state, network;
TRUNCATE payment_logs_daily_rollup;
INSERT INTO payment_logs_daily_rollup (day, tool_name, user_agent, state, network, n)
    SELECT day, tool_name, user_agent, state, network, n FROM rollup_compact;
DROP TABLE rollup_compact;
COMMIT;
-- Expect: SELECT count(*) FROM payment_logs_daily_rollup;  → ~8–10k (was 139,848)
-- and     SELECT sum(n)   FROM payment_logs_daily_rollup;  → unchanged.

-- ── B) payment_logs bloat — the dashboard's numbers (2026-09-01) ─────────
-- Observability → Database → Large objects: payment_logs 77.3 MB heap for
-- 63,862 rows (~1.2 KB/row: every legacy row was UPDATEd pending→abandoned,
-- so the heap is mostly dead-tuple space), plus payment_logs_payment_id_key
-- 12.3 MB, idx_payment_logs_agent 11.3 MB and idx_payment_logs_state 10.3 MB.
-- An index on `state` therefore already EXISTS — nothing to create here.
-- Cache hit rate is 100%, so the reads are served from RAM; what the
-- budget pays for is WRITES (rollup inserts, pending_challenges
-- insert+delete churn for monitors' paid-tool 402s, autovacuum over the
-- bloated heap). Block C shrinks the heap; block D reclaims it.

-- ── C) OPTIONAL — drop the legacy bot-probe rows (IRREVERSIBLE) ──────────
-- 61,105 'abandoned' rows created before fix #2 are pre-08-04 bot 402s
-- whose only value was the "who probes us" signal, which the rollup has
-- carried since 08-04. Export first if you want the raw rows:
--   python tools/export_abandoned_legacy.py  →  notes/payment_logs_abandoned_<date>.csv
-- Then:
-- DELETE FROM payment_logs WHERE state = 'abandoned' AND created_at < '2026-08-21';
-- Expect: SELECT count(*) FROM payment_logs;  → ~1.1k (was 63,862).

-- ── D) Afterwards (either way) — run EACH LINE ALONE ─────────────────────
-- VACUUM refuses to run inside a transaction, and the SQL editor wraps a
-- multi-statement run in one; paste one line, run, then the next.
-- VACUUM (ANALYZE) payment_logs_daily_rollup;
-- VACUUM (ANALYZE) payment_logs;
-- Optional, only after block C, to give the 77 MB heap back to the OS
-- (plain VACUUM only marks it reusable). Takes an exclusive lock for a few
-- seconds — a settle landing in that window would retry/log an insert
-- error, so run it at a quiet hour:
-- VACUUM FULL payment_logs;
-- REINDEX TABLE CONCURRENTLY payment_logs;   -- no write lock; shrinks the 3 bloated indexes
