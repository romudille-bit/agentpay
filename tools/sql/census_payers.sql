-- census_payers.sql — AGE-140 multi-provider buyer census (Base, per-payer aggregates)
--
-- DENOMINATOR (state it in every report):
--   USDC transfers on Base in the last 30 days that were settled through an
--   EIP-3009 authorization. The USDC contract emits AuthorizationUsed(authorizer,
--   nonce) on every transferWithAuthorization / receiveWithAuthorization, which is
--   the x402 "exact" scheme fingerprint on EVM — independent of which facilitator
--   submitted the transaction, and with no payTo/catalog list involved (so
--   off-catalog providers are included).
--   tx sender        = facilitator relayer (CDP, PayAI, …)
--   Transfer.from    = the real payer (== authorizer)
--   Transfer.to      = the provider's payTo
--
-- Caveat: EIP-3009 is not exclusively x402 (some wallets use gasless USDC sends).
-- The relayer column lets you see how much of the set is submitted by known x402
-- facilitators; the script reports that share.
--
-- Window is HARDCODED (30 days) — Dune parameter substitution bit us on the first run
-- ("Invalid INTERVAL DAY value: default value"). Create as a saved query, note the id,
-- and run the script with --no-params. Edit the three interval lines to change the window.

WITH auth AS (
    SELECT tx_hash,
           bytearray_substring(topic1, 13, 20) AS authorizer
    FROM base.logs
    WHERE contract_address = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
      AND topic0 = 0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5
      AND block_time >= now() - interval '30' day
),
xfer AS (
    SELECT evt_tx_hash AS tx_hash,
           "from"       AS payer,
           "to"         AS recipient,
           CAST(value AS double) / 1e6 AS usd,
           evt_block_time AS block_time
    FROM erc20_base.evt_Transfer
    WHERE contract_address = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
      AND evt_block_time >= now() - interval '30' day
),
legs AS (
    SELECT x.tx_hash, x.payer, x.recipient, x.usd, x.block_time, tx."from" AS relayer
    FROM xfer x
    JOIN auth a
      ON a.tx_hash = x.tx_hash AND a.authorizer = x.payer
    JOIN base.transactions tx
      ON tx.hash = x.tx_hash
     AND tx.block_time >= now() - interval '30' day
),
per_payer AS (
    SELECT payer,
           COUNT(*)                  AS legs,
           SUM(usd)                  AS usd,
           COUNT(DISTINCT recipient) AS recipients,
           COUNT(DISTINCT relayer)   AS relayers,
           MIN(block_time)           AS first_leg,
           MAX(block_time)           AS last_leg
    FROM legs
    GROUP BY payer
),
per_relayer AS (
    SELECT payer, relayer, COUNT(*) AS n,
           ROW_NUMBER() OVER (PARTITION BY payer ORDER BY COUNT(*) DESC) AS rn
    FROM legs
    GROUP BY payer, relayer
)
SELECT p.payer,
       p.legs,
       p.usd,
       p.recipients,
       p.relayers,
       r.relayer AS top_relayer,
       p.first_leg,
       p.last_leg
FROM per_payer p
LEFT JOIN per_relayer r ON r.payer = p.payer AND r.rn = 1
ORDER BY p.usd DESC
