-- AGE-95: keep the sBTC quote issued with a 402 on the challenge row, so a
-- settle after a gateway restart verifies against the issued quote instead
-- of re-quoting at the current rate. Apply before deploying a gateway with
-- STACKS_ENABLED=true; NULL for every non-Stacks challenge.
ALTER TABLE pending_challenges
    ADD COLUMN IF NOT EXISTS stacks_sats bigint,
    ADD COLUMN IF NOT EXISTS stacks_rate text;
