-- census_pairs_x402.sql — AGE-140 multi-provider buyer census, x402-SCOPED version (v2)
--
-- v1 (census_payers.sql / census_pairs.sql) used every EIP-3009 USDC transfer on
-- Base as the denominator and came back with $35M / 10.4M legs / 65k wallets in
-- 30 days — ~25x the x402 market (x402scan $1.24M, fuchss $1.46M MTD). The CDP
-- relayers (0x97ac…a016 alone = 78% of legs) also submit Base Pay / consumer
-- gasless USDC sends, so EIP-3009 + facilitator relayer is NOT an x402 fingerprint
-- on its own. v2 adds two filters, both stated in the report:
--   1. tx sender ∈ the 128 Base relayer addresses registered in x402scan's open
--      facilitator registry (Merit-Systems/x402scan, packages/external/facilitators)
--   2. recipient is a "metered seller": ≥5 distinct payers in the window and mean
--      leg ≤ $1 — i.e. it is being paid per API call, not receiving purchases.
-- Excluded on purpose (say so): Bitrefill-style commerce ($136/payer), $1+ inference
-- sellers (Cluster, CheapTokens), and sellers with <5 payers. Sensitivity: re-run
-- with AVG(usd) <= 5 and >= 2 payers to see how much the picture moves.
-- Window hardcoded (30 days). Run the script with --no-params.

WITH relayers AS (
    SELECT addr FROM (VALUES
        0x001ddabba5782ee48842318bd9ff4008647c8d9c,
        0x0168f80e035ea68b191faf9bfc12778c87d92008,
        0x021cc47adeca6673def958e324ca38023b80a5be,
        0x03a3f7ce8e21e6f8d9fa14c67d8876b2470dc2f1,
        0x052aaae3cad5c095850246f8ffb228354c56752a,
        0x06f0bfd2c8f36674df5cde852c1eed8025c268c9,
        0x103040545ac5031a11e8c03dd11324c7333a13c7,
        0x1363c7ff51ccce10258a7f7bddd63baab6aaf678,
        0x14fda13953fc30428938e6bf950d036e77214e52,
        0x15e2e2da7539ef1f652aa3c1d6142a535aa3d7ea,
        0x16e47d275198ed65916a560bab4af6330c36ae09,
        0x179761d9eed0f0d1599330cc94b0926e68ae87f1,
        0x1892f72fdb3a966b2ad8595aa5f7741ef72d6085,
        0x1fc230ee3c13d0d520d49360a967dbd1555c8326,
        0x222c4367a2950f3b53af260e111fc3060b0983ff,
        0x24d4f332d8e886fc005bb4a103bad21d9ebc2b7f,
        0x25659315106580ce2a787ceec5efb2d347b539c9,
        0x279e08f711182c79ba6d09669127a426228a4653,
        0x290d8b8edcafb25042725cb9e78bcac36b8865f8,
        0x2a89407a98a0732b7fd578c4e156b7166540eb5a,
        0x2bb201f1bb056eb738718bd7a3ad1bef24b883bb,
        0x2daaef6f941de214bf7d6daf322bc6bc7406accb,
        0x2fae4026a31f19183947f0a6045ef975ebfa9ca8,
        0x3210d7b21bfe1083c9dddbe17e8f947c9029a584,
        0x37dfb4033d5dd98fd335f24d0d42e8fe68d587d6,
        0x3a5ca1c6aa6576ae9c1c0e7fa2b4883346bc5aa0,
        0x3a70788150c7645a21b95b7062ab1784d3cc2104,
        0x3be45f576696a2fd5a93c1330cd19f1607ab311d,
        0x3f61093f61817b29d9556d3b092e67746af8cdfd,
        0x40272e2eac848ea70db07fd657d799bd309329c4,
        0x402feee072d655b85e08f1751af9ddbcd249521f,
        0x42dd53906b49c202e8e934b059dc019e04634b00,
        0x4544b535938b67d2a410a98a7e3b0f8f68921ca7,
        0x4638bc811c93bf5e60deed32325e93505f681576,
        0x47d8b3c9717e976f31025089384f23900750a5f4,
        0x489c40fc3c2a19ad8cb275b7dd6aa194e9219c4f,
        0x48ab4b0af4ddc2f666a3fcc43666c793889787a3,
        0x4c934c63c786157fefd990945b25ea60a0fb0205,
        0x4ffeffa616a1460570d1eb0390e264d45a199e91,
        0x51fec16843e49b99aaf9814e525aee1756e66a62,
        0x552300992857834c0ad41c8e1a6934a5e4a2e4ca,
        0x59b7ebc67a3d627fabaf06768c818638452ae704,
        0x59e8014a3b884392fbb679fe461da07b18c1ff81,
        0x5e437bee4321db862ac57085ea5eb97199c0ccc5,
        0x612d72dc8402bba997c61aa82ce718ea23b2df5d,
        0x625d8a65134079f8faaac39a7947c73d93c6ac39,
        0x64cc42b1ce598e3abcfbb64df4688521ddbf1f0a,
        0x65058cf664d0d07f68b663b0d4b4f12a5e331a38,
        0x66c40946b0dffd04be467e18309857307ecd37cb,
        0x675707bc7d03089f820c1b7d49f7480083e8f4df,
        0x67b9ce703d9ce658d7c4ac3c289cea112fe662af,
        0x6831508455a716f987782a1ab41e204856055cc2,
        0x68a96f41ff1e9f2e7b591a931a4ad224e7c07863,
        0x68efafe862d89ce66dd3d7b07d5a3747a0871164,
        0x6ccf245c883f9f3c6caee0687aa61daf7bc96e32,
        0x708e57b6650a9a741ab39cae1969ea1d2d10eca1,
        0x724efafb051f17ae824afcdf3c0368ae312da264,
        0x73b2b8df52fbe7c40fe78db52e3dffdd5db5ad07,
        0x76eee8f0acabd6b49f1cc4e9656a0c8892f3332e,
        0x772003a2e9c2ccc8af956870a37a66f64f8cec38,
        0x7c766f5fd9ab3dc09acad5ecfacc99c4781efe29,
        0x7e20b62bf36554b704774afb0fcc0ae8f899213b,
        0x7f6d822467df2a85f792d4508c5722ade96be056,
        0x7f72a02c682e908d46a5677fe937cdb612d94a3b,
        0x80735b3f7808e2e229ace880dbe85e80115631ca,
        0x80c08de1a05df2bd633cf520754e40fde3c794d3,
        0x87af99356d774312b73018b3b6562e1ae0e018c9,
        0x88800e08e20b45c9b1f0480cf759b5bf2f05180c,
        0x88e13d4c764a6c840ce722a0a3765f55a85b327e,
        0x8cda367232d78c067116e3260da881d2da8ffa39,
        0x8d8fa42584a727488eeb0e29405ad794a105bb9b,
        0x8e7769d440b3460b92159dd9c6d17302b036e2d6,
        0x8f5cb67b49555e614892b7233cfddebfb746e531,
        0x90d5e567017f6c696f1916f4365dd79985fce50f,
        0x90da501fdbec74bb0549100967eb221fed79c99b,
        0x91d313853ad458addda56b35a7686e2f38ff3952,
        0x91ddea05f741b34b63a7548338c90fc152c8631f,
        0x93f6601151ccb08f333ab4b1cccfb1e188c0be44,
        0x94701e1df9ae06642bf6027589b8e05dc7004813,
        0x97316fa4730bc7d3b295234f8e4d04a0a4c093e8,
        0x97acce27d5069544480bde0f04d9f47d7422a016,
        0x97d38aa5de015245dcca76305b53abe6da25f6a5,
        0x97db9b5291a218fc77198c285cefdc943ef74917,
        0x9aae2b0d1b9dc55ac9bab9556f9a26cb64995fb9,
        0x9c09faa49c4235a09677159ff14f17498ac48738,
        0x9df61a719ddae27c20a63a417271cc2c704654bd,
        0x9fb2714af0a84816f5c6322884f2907e33946b88,
        0xa1822b21202a24669eaf9277723d180cd6dae874,
        0xa32ccda98ba7529705a059bd2d213da8de10d101,
        0xa9a54ef09fc8b86bc747cec6ef8d6e81c38c6180,
        0xaa0df01e4d11decf2ad2c459c81d3a495e4f1925,
        0xaaca1ba9d2627cbc0739ba69890c30f95de046e4,
        0xadd5585c776b9b0ea77e9309c1299a40442d820f,
        0xaf990eef9846b63d896056050fdc0b28bca9c24b,
        0xb2bd29925cbbcea7628279c91945ca5b98bf371b,
        0xb578b7db22581507d62bdbeb85e06acd1be09e11,
        0xb5d25e1fa0718bf3e1bf698f96791d4e93632ec8,
        0xb70c4fe126de09bd292fe3d1e40c6d264ca6a52a,
        0xb87e1a2cc2b4643f2892768e80e41167f17c5860,
        0xb8f41cb13b1f213da1e94e1b742ec1323235c48f,
        0xc19829b32324f116ee7f80d193f99e445968499a,
        0xc6699d2aada6c36dfea5c248dd70f9cb0235cb63,
        0xc67b555b4a9d340ed7c5d87743163c31a75f2254,
        0xca5e87f82b3fa093800e6ad67d621a427d79c70d,
        0xcbb10c30a9a72fae9232f41cbbd566a097b4e03a,
        0xce7819f0b0b871733c933d1f486533bab95ec47b,
        0xce82eeec8e98e443ec34fda3c3e999cbe4cb6ac2,
        0xd2f74a14522d40e4a1d7fbb62aa97ce99fa1a7e5,
        0xd348e724e0ef36291a28dfeccf692399b0e179f8,
        0xd744494e28b01073514ebc89987b305001ed257a,
        0xd7469bf02d221968ab9f0c8b9351f55f8668ac4f,
        0xd7d91a42dfadd906c5b9ccde7226d28251e4cd0f,
        0xd88a9a58806b895ff06744082c6a20b9d7184b0f,
        0xd8dfc729cbd05381647eb5540d756f4f8ad63eec,
        0xd97c12726dcf994797c981d31cfb243d231189fb,
        0xdbdf3d8ed80f84c35d01c6c9f9271761bad90ba6,
        0xdc8fbad54bf5151405de488f45acd555517e0958,
        0xe07e9cbf9a55d02e3ac356ed4706353d98c5a618,
        0xe299c486066739c4a31609e1268d93229632dd47,
        0xe575fa51af90957d66fab6d63355f1ed021b887b,
        0xe6123e6b389751c5f7e9349f3d626b105c1fe618,
        0xe72f0af4cf41356d433723547f1412ca27fbb1b8,
        0xe74817f4cdc15844314812b2271276e64e890fae,
        0xea52f2c6f6287f554f9b54c5417e1e431fe5710e,
        0xec10243b54df1a71254f58873b389b7ecece89c2,
        0xf46833d4ac4f0f1405cc05c30edfd86770f721c9,
        0xf70e7cb30b132fab2a0a5e80d41861aa133ea21b,
        0xfe0920a0a7f0f8a1ec689146c30c3bbef439bf8a
    ) AS t(addr)
),
auth AS (
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
legs_all AS (
    SELECT x.tx_hash, x.payer, x.recipient, x.usd, x.block_time, tx."from" AS relayer
    FROM xfer x
    JOIN auth a
      ON a.tx_hash = x.tx_hash AND a.authorizer = x.payer
    JOIN base.transactions tx
      ON tx.hash = x.tx_hash
     AND tx.block_time >= now() - interval '30' day
    JOIN relayers r
      ON r.addr = tx."from"
),
-- A "metered seller" is a recipient that looks like an API being paid per call:
-- at least 5 distinct payers in the window and a mean leg of at most $1.
-- This is what separates x402 tool calls from the consumer/commerce USDC sends
-- that share the same facilitator relayers (Base Pay, gift cards, transfers).
sellers AS (
    SELECT recipient
    FROM legs_all
    GROUP BY recipient
    HAVING COUNT(DISTINCT payer) >= 5 AND AVG(usd) <= 1.0
),
legs AS (
    SELECT l.*
    FROM legs_all l
    JOIN sellers s ON s.recipient = l.recipient
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
