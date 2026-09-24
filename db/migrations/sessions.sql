-- Server-side session cap (AGE-207). Apply once in the Supabase SQL Editor
-- before setting SESSION_ENFORCEMENT=true on a gateway. Idempotent.
--
-- A session belongs to the address that paid for it (payer, network). Every
-- priced call from that address is reserved against the session's cap
-- BEFORE the gateway settles anything; the reservation is atomic in
-- consume_session_budget, so concurrent calls cannot overspend together.

CREATE TABLE IF NOT EXISTS sessions (
    session_id   uuid PRIMARY KEY,
    payer        text NOT NULL,
    network      text NOT NULL,          -- 'stacks-mainnet', 'base-mainnet', ...
    max_spend    numeric(18, 6) NOT NULL CHECK (max_spend >= 0),
    spent        numeric(18, 6) NOT NULL DEFAULT 0 CHECK (spent >= 0),
    status       text NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'exhausted', 'expired', 'revoked')),
    label        text,
    payment_id   text,                   -- the session_create settle (tx or challenge id)
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL
);

-- One live session per payer: the unique partial index is what makes
-- "a new session can't replace an active one" hold under concurrent creates.
CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_active_per_payer
    ON sessions (payer) WHERE status = 'active';

ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS session_id uuid;
CREATE INDEX IF NOT EXISTS payment_logs_session_id_idx
    ON payment_logs (session_id) WHERE session_id IS NOT NULL;

-- Reserve p_cost against the payer's live session (active, or exhausted and
-- not yet expired — an exhausted cap keeps refusing until a new session is
-- paid for), or against p_session_id when the caller names one. One row:
--   found = false                → no live session for this payer (unmetered)
--   found = true, ok = true      → reserved; spent is the new total
--   found = true, ok = false     → over cap, exhausted, or not this payer's
-- p_force records the cost even past the cap (a rail where the payment
-- already happened): ok = true, status 'exhausted'.
-- p_floor is the cheapest priced call: once less than that remains, the
-- session is 'exhausted' (nothing can be bought) and the payer may open a
-- new one instead of waiting for expiry.
DROP FUNCTION IF EXISTS consume_session_budget(text, numeric, uuid);
DROP FUNCTION IF EXISTS consume_session_budget(text, numeric, uuid, boolean);
CREATE OR REPLACE FUNCTION consume_session_budget(
    p_payer text, p_cost numeric, p_session_id uuid DEFAULT NULL,
    p_force boolean DEFAULT false, p_floor numeric DEFAULT 0
) RETURNS TABLE (r_found boolean, r_ok boolean, r_session_id uuid,
                 r_max_spend numeric, r_spent numeric, r_expires_at timestamptz)
LANGUAGE plpgsql AS $$
DECLARE
    s sessions%ROWTYPE;
BEGIN
    SELECT * INTO s FROM sessions
     WHERE sessions.status IN ('active', 'exhausted')
       AND (p_session_id IS NULL OR sessions.session_id = p_session_id)
       AND (p_session_id IS NOT NULL OR sessions.payer = p_payer)
     ORDER BY (sessions.status = 'active') DESC, sessions.created_at DESC
     LIMIT 1
     FOR UPDATE;
    IF s.session_id IS NULL THEN
        RETURN QUERY SELECT false, false, NULL::uuid, NULL::numeric, NULL::numeric, NULL::timestamptz;
        RETURN;
    END IF;
    IF s.payer <> p_payer THEN
        RETURN QUERY SELECT true, false, s.session_id, s.max_spend, s.spent, s.expires_at;
        RETURN;
    END IF;
    IF s.expires_at <= now() THEN
        UPDATE sessions SET status = 'expired' WHERE sessions.session_id = s.session_id;
        RETURN QUERY SELECT false, false, s.session_id, s.max_spend, s.spent, s.expires_at;
        RETURN;
    END IF;
    IF s.spent + p_cost > s.max_spend AND NOT p_force THEN
        IF s.max_spend - s.spent < p_floor THEN
            UPDATE sessions SET status = 'exhausted' WHERE sessions.session_id = s.session_id;
        END IF;
        RETURN QUERY SELECT true, false, s.session_id, s.max_spend, s.spent, s.expires_at;
        RETURN;
    END IF;
    UPDATE sessions SET spent = sessions.spent + p_cost,
                        status = CASE WHEN sessions.max_spend - (sessions.spent + p_cost) < p_floor
                                        OR sessions.spent + p_cost >= sessions.max_spend
                                      THEN 'exhausted' ELSE sessions.status END
     WHERE sessions.session_id = s.session_id
     RETURNING sessions.spent INTO s.spent;
    RETURN QUERY SELECT true, true, s.session_id, s.max_spend, s.spent, s.expires_at;
END $$;

-- Give a reservation back after a settle that charged nothing. An
-- 'exhausted' session reopens unless the payer has since opened another
-- one (the partial index allows one active per payer); 'expired'/'revoked'
-- stay closed.
CREATE OR REPLACE FUNCTION release_session_budget(p_session_id uuid, p_cost numeric)
RETURNS numeric LANGUAGE plpgsql AS $$
DECLARE
    new_spent numeric;
BEGIN
    UPDATE sessions
       SET spent  = GREATEST(sessions.spent - p_cost, 0),
           status = CASE WHEN sessions.status = 'exhausted'
                          AND NOT EXISTS (SELECT 1 FROM sessions o
                                           WHERE o.payer = sessions.payer
                                             AND o.status = 'active'
                                             AND o.session_id <> sessions.session_id)
                         THEN 'active' ELSE sessions.status END
     WHERE sessions.session_id = p_session_id
     RETURNING sessions.spent INTO new_spent;
    RETURN new_spent;
END $$;

-- Sweep: close sessions past their expiry so the partial index frees the payer.
CREATE OR REPLACE FUNCTION expire_sessions() RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    n integer;
BEGIN
    UPDATE sessions SET status = 'expired'
     WHERE status IN ('active', 'exhausted') AND expires_at <= now();
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- Private table: the gateway's secret key bypasses RLS; the anon key gets
-- nothing (no policy). The functions move money-shaped state, so only the
-- gateway may call them.
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
REVOKE EXECUTE ON FUNCTION consume_session_budget(text, numeric, uuid, boolean, numeric) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION release_session_budget(uuid, numeric) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION expire_sessions() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE EXECUTE ON FUNCTION consume_session_budget(text, numeric, uuid, boolean, numeric),
                                   release_session_budget(uuid, numeric),
                                   expire_sessions() FROM anon, authenticated;
        GRANT EXECUTE ON FUNCTION consume_session_budget(text, numeric, uuid, boolean, numeric),
                                  release_session_budget(uuid, numeric),
                                  expire_sessions() TO service_role;
    END IF;
END $$;
