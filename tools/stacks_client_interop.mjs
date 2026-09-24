// Pays pre_trade_check on agentpay.tools once through each published Stacks
// x402 client (x402-stacks, AIBTC MCP wallet) from the demo payer, and saves
// the txids as evidence. Run from a folder with the two clients installed:
//   npm i @aibtc/mcp-server@1.71.0 x402-stacks@2.0.3 axios
//   node ~/Projects/agentpay/tools/stacks_client_interop.mjs
// The Secret Key is read from a hidden prompt and never written anywhere.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const BASE = process.env.AGENTPAY_URL || "https://agentpay.tools";
const EXPECTED = process.env.EXPECTED_ADDRESS || "SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5";
const SBTC = "SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token::sbtc-token";
const CALL = ["/tools/pre_trade_check/call", { parameters: { symbol: "BTC", side: "long", size_usd: 1000 } }];

function askHidden(q) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    rl._writeToOutput = (s) => { if (s.includes(q)) rl.output.write(s); };
    rl.question(q, (a) => { rl.close(); process.stdout.write("\n"); resolve(a.trim()); });
  });
}

function stop(msg) { console.error(`\nStopped: ${msg}`); process.exit(1); }

// The flagship pays from this wallet daily at 13:00 UTC; overlapping would clash on the nonce.
const now = new Date();
const minutes = now.getUTCHours() * 60 + now.getUTCMinutes();
if (!process.env.SKIP_GUARDS && minutes >= 12 * 60 + 45 && minutes <= 13 * 60 + 30)
  stop("too close to the flagship's 13:00 UTC run. Try again after 13:30 UTC.");

process.env.NETWORK = "mainnet";
const aibtc = await import(pathToFileURL(path.resolve("node_modules/@aibtc/mcp-server/dist/services/x402.service.js")).href);
const req = createRequire(path.join(process.cwd(), "noop.js"));  // resolve from the run folder, not this file's
const x402stacks = req("x402-stacks");
const axios = req("axios");

let words = await askHidden("Demo payer Secret Key (24 words, hidden): ");
const acct = await aibtc.mnemonicToAccount(words, "mainnet");
if (acct.address !== EXPECTED)
  stop(`the first account of this Secret Key is ${acct.address}, not ${EXPECTED}. ` +
       "AIBTC's client only uses the first account, so nothing was paid.");

if (!process.env.SKIP_GUARDS) {
  const bal = (await axios.get(`https://api.hiro.so/extended/v1/address/${EXPECTED}/balances`)).data;
  const sats = Number(bal.fungible_tokens?.[SBTC]?.balance || 0);
  const stx = Number(bal.stx?.balance || 0) / 1e6;
  console.log(`Demo payer ${EXPECTED}: ${sats} sats sBTC, ${stx} STX`);
  if (sats < 100 || stx < 0.1) stop("needs at least 100 sats sBTC and 0.1 STX.");
}

const results = [];
async function run(client, version, pay) {
  console.log(`\n→ ${client} ${version}: paying ${BASE}${CALL[0]} …`);
  try {
    const res = await pay();
    const p = res.data?.payment || {};
    const txid = p.tx_hash?.startsWith("0x") ? p.tx_hash : `0x${p.tx_hash}`;
    console.log(`  ok  tx ${txid}\n      binding=${p.binding} payer_protection=${p.payer_protection}`);
    console.log(`      https://explorer.hiro.so/txid/${txid}?chain=mainnet`);
    results.push({ client, version, ok: true, txid, network: p.network, binding: p.binding,
                   payer_protection: p.payer_protection, amount_usdc: p.amount_usdc });
  } catch (e) {
    const body = e.response?.data ? JSON.stringify(e.response.data).slice(0, 300) : "";
    console.log(`  FAILED: ${e.message} ${body}`);
    results.push({ client, version, ok: false, error: `${e.message} ${body}`.trim() });
  }
}

await run("x402-stacks", "2.0.3", () => {
  const account = x402stacks.privateKeyToAccount(acct.privateKey, "mainnet");
  return x402stacks.wrapAxiosWithPayment(axios.create({ baseURL: BASE, timeout: 180000 }), account).post(...CALL);
});

process.env.CLIENT_MNEMONIC = words;
await run("@aibtc/mcp-server", "1.71.0", async () => (await aibtc.createApiClient(BASE)).post(...CALL));
delete process.env.CLIENT_MNEMONIC;
words = "";

const out = path.join(os.homedir(), "Projects/agentpay/notes",
                      `stacks_standard_clients_mainnet_${now.toISOString().slice(0, 10)}.json`);
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, JSON.stringify({ gateway: BASE, payer: EXPECTED, at: now.toISOString(), results }, null, 2));
console.log(`\nEvidence saved to ${out}`);
process.exit(results.every((r) => r.ok) ? 0 : 1);
