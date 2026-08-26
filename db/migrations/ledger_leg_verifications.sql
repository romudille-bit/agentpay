-- ledger_leg_verifications — AGE-142 (2026-08-26)
--
-- Chain-verified evidence for the paid legs of receipt-derived ledger runs
-- (probe sweeps and strategy runs that settle agent→seller, off gateway).
-- /ledger could only verify legs that settled THROUGH the gateway
-- (payment_logs); every off-gateway leg rendered as `agent_attested` — ~79%
-- of the flagship's lifetime spend. The payer-side truth is on Base anyway:
-- transferWithAuthorization emits a USDC Transfer FROM our wallet whoever
-- submits the tx. gateway/services/leg_verifier.py pulls the run wallet's
-- outbound transfers in the run window, matches them to the receipt legs,
-- and caches the result here so the ledger render never queries the chain.
--
-- One row per verified paid leg, keyed on the run's run_at + the leg's
-- 1-based index in the receipt breakdown. A marker row with leg_index = -1
-- records that the run was checked (so unmatched runs aren't re-queried every
-- cycle); `method` on the marker is 'checked' and tx_hash is NULL.
--
-- method:
--   hash          — the leg carried a tx_hash and that tx is in the wallet's
--                   outbound transfers (strongest)
--   amount+payto  — no hash on the leg; matched a transfer with the same
--                   amount to the seller's known payTo (from service_probes)
--   amount        — matched a same-amount transfer from the run wallet in the
--                   run window (weakest; the wallet paid this amount on-chain
--                   in the window, attribution to THIS leg is by elimination)
--   checked       — marker row (leg_index = -1); no evidence for the rest

CREATE TABLE IF NOT EXISTS ledger_leg_verifications (
    run_at       timestamptz NOT NULL,
    leg_index    integer     NOT NULL,
    tx_hash      text,
    to_addr      text,
    amount_usdc  numeric,
    wallet       text,
    network      text,
    method       text        NOT NULL,
    verified_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_at, leg_index)
);

CREATE INDEX IF NOT EXISTS ledger_leg_verifications_run_at_idx
    ON ledger_leg_verifications (run_at DESC);

ALTER TABLE ledger_leg_verifications ENABLE ROW LEVEL SECURITY;

-- Public read (the ledger is a public proof point); writes only via the
-- service role the gateway holds — same posture as service_probes.
DROP POLICY IF EXISTS "public read ledger_leg_verifications" ON ledger_leg_verifications;
CREATE POLICY "public read ledger_leg_verifications"
    ON ledger_leg_verifications FOR SELECT USING (true);
