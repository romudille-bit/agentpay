-- AGE-208: keep the native-STX quote issued with a 402 on the challenge row,
-- next to the sBTC one (pending_challenges_stacks_quote.sql). Apply before
-- setting STACKS_STX=true; NULL for every challenge without an STX option.
ALTER TABLE pending_challenges
    ADD COLUMN IF NOT EXISTS stacks_ustx bigint,
    ADD COLUMN IF NOT EXISTS stx_usd_rate text;
