-- census_pairs.sql — AGE-140 multi-provider buyer census (payer × recipient pairs)
--
-- Same denominator as census_payers.sql (EIP-3009-settled USDC transfers on Base,
-- last 30 days). Returns one row per (payer, recipient) pair, restricted to
-- payers that paid at least 2 distinct recipients — the
-- population the "we are the total" thesis is about. Used for:
--   * prober vs buyer split (legs per recipient)
--   * provider-resolution rate (which recipients we can name)
--   * per-buyer provider lists in the internal report
--
-- Window (30 days) and min_recipients (2) are HARDCODED — see census_payers.sql.
-- Run the script with --no-params.

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
    SELECT x.tx_hash, x.payer, x.recipient, x.usd, x.block_time
    FROM xfer x
    JOIN auth a
      ON a.tx_hash = x.tx_hash AND a.authorizer = x.payer
),
multi AS (
    SELECT payer
    FROM legs
    GROUP BY payer
    HAVING COUNT(DISTINCT recipient) >= 2
)
SELECT l.payer,
       l.recipient,
       COUNT(*)        AS legs,
       SUM(l.usd)      AS usd,
       MIN(l.block_time) AS first_leg,
       MAX(l.block_time) AS last_leg
FROM legs l
JOIN multi m ON m.payer = l.payer
GROUP BY l.payer, l.recipient
ORDER BY l.payer, usd DESC
