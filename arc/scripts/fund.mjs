// Bridges USDC from Base to the probe address on Arc with CCTP V2 + Circle's forwarding service
// (Circle submits the Arc mint, so the recipient needs no gas). Key comes from FUND_KEY_ENV (default
// AGENT_BASE_KEY_TEST in the repo .env) and is never printed.
//   node --env-file-if-exists=../.env --env-file-if-exists=.env scripts/fund.mjs 1.50 [--dry] [--to 0x…]
import { createPublicClient, createWalletClient, encodeFunctionData, erc20Abi, formatUnits, http, pad, parseUnits } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { base } from "viem/chains";
import { txUrl } from "../src/lib.mjs";

const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const TOKEN_MESSENGER = "0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d"; // CCTP V2, same address on Base and Arc
const BASE_DOMAIN = 6, ARC_DOMAIN = 26;
const IRIS = "https://iris-api.circle.com";
const FORWARD_HOOK = "0x636374702d666f72776172640000000000000000000000000000000000000000"; // "cctp-forward"
const FAST = 1000;

const args = process.argv.slice(2);
const DRY = args.includes("--dry");
const amountUsd = args.find((a) => /^\d+(\.\d+)?$/.test(a));
if (!amountUsd) { console.error("usage: fund.mjs <amount USDC> [--dry] [--to 0x…]"); process.exit(1); }
const keyEnv = process.env.FUND_KEY_ENV ?? "AGENT_BASE_KEY_TEST";
const key = process.env[keyEnv];
if (!key) { console.error(`${keyEnv} missing`); process.exit(1); }
const to = args.includes("--to") ? args[args.indexOf("--to") + 1] : process.env.ARC_PRIVATE_KEY && privateKeyToAccount(process.env.ARC_PRIVATE_KEY).address;
if (!/^0x[0-9a-fA-F]{40}$/.test(to ?? "")) { console.error("recipient unknown: pass --to or set ARC_PRIVATE_KEY"); process.exit(1); }

const account = privateKeyToAccount(key);
const pub = createPublicClient({ chain: base, transport: http() });
const wallet = createWalletClient({ account, chain: base, transport: http() });

const amount = parseUnits(amountUsd, 6);
const fees = await (await fetch(`${IRIS}/v2/burn/USDC/fees/${BASE_DOMAIN}/${ARC_DOMAIN}?forward=true`)).json();
const tier = fees.find((f) => f.finalityThreshold === FAST);
const protocolFee = (amount * BigInt(Math.round(tier.minimumFee * 100))) / 1_000_000n;
const maxFee = ((BigInt(tier.forwardFee.med) + protocolFee) * 11n) / 10n; // 10% headroom; unused fee is not charged
const burn = amount + maxFee;

const [usdc, eth, allowance] = await Promise.all([
  pub.readContract({ address: BASE_USDC, abi: erc20Abi, functionName: "balanceOf", args: [account.address] }),
  pub.getBalance({ address: account.address }),
  pub.readContract({ address: BASE_USDC, abi: erc20Abi, functionName: "allowance", args: [account.address, TOKEN_MESSENGER] }),
]);
console.log(`from  ${account.address}  ${formatUnits(usdc, 6)} USDC, ${formatUnits(eth, 18)} ETH on Base`);
console.log(`to    ${to} on Arc`);
console.log(`send  ${amountUsd} USDC, burn ${formatUnits(burn, 6)} (max fee ${formatUnits(maxFee, 6)}: forward ${formatUnits(BigInt(tier.forwardFee.med), 6)} + protocol ${formatUnits(protocolFee, 6)})`);
if (usdc < burn) { console.error(`insufficient USDC: need ${formatUnits(burn, 6)}`); process.exit(1); }
if (eth === 0n) { console.error("no ETH for gas on Base"); process.exit(1); }
if (DRY) { console.log("dry run, nothing sent"); process.exit(0); }

if (allowance < burn) {
  const h = await wallet.writeContract({ address: BASE_USDC, abi: erc20Abi, functionName: "approve", args: [TOKEN_MESSENGER, burn] });
  await pub.waitForTransactionReceipt({ hash: h });
  console.log(`approve https://basescan.org/tx/${h}`);
}
const burnTx = await wallet.sendTransaction({
  to: TOKEN_MESSENGER,
  data: encodeFunctionData({
    abi: [{ type: "function", name: "depositForBurnWithHook", stateMutability: "nonpayable", outputs: [], inputs: [
      { name: "amount", type: "uint256" }, { name: "destinationDomain", type: "uint32" }, { name: "mintRecipient", type: "bytes32" },
      { name: "burnToken", type: "address" }, { name: "destinationCaller", type: "bytes32" }, { name: "maxFee", type: "uint256" },
      { name: "minFinalityThreshold", type: "uint32" }, { name: "hookData", type: "bytes" } ] }],
    functionName: "depositForBurnWithHook",
    args: [burn, ARC_DOMAIN, pad(to, { size: 32 }), BASE_USDC, pad("0x", { size: 32 }), maxFee, FAST, FORWARD_HOOK],
  }),
});
console.log(`burn  https://basescan.org/tx/${burnTx}`);
await pub.waitForTransactionReceipt({ hash: burnTx });

process.stdout.write("waiting for Circle to attest and mint on Arc");
for (let i = 0; i < 120; i++) {
  await new Promise((r) => setTimeout(r, 5000));
  const r = await fetch(`${IRIS}/v2/messages/${BASE_DOMAIN}?transactionHash=${burnTx}`);
  const m = r.ok ? (await r.json()).messages?.[0] : null;
  if (m?.forwardTxHash) { console.log(`\nmint  ${txUrl(m.forwardTxHash)}`); process.exit(0); }
  process.stdout.write(".");
}
console.log(`\nno mint yet; check ${IRIS}/v2/messages/${BASE_DOMAIN}?transactionHash=${burnTx}`);
