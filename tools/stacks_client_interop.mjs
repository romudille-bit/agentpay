// Pays pre_trade_check on agentpay.tools once through each published Stacks
// x402 client and saves the txids as evidence:
//   x402-stacks pays from the demo payer (found by index on the Leather Secret Key);
//   AIBTC only uses account 0 of CLIENT_MNEMONIC, so it pays from a throwaway key
//   generated here, funded by the demo payer and swept back afterwards.
// The Leather Secret Key never reaches AIBTC. No words are printed or saved.
// Run from a folder with the clients installed:
//   npm i @aibtc/mcp-server@1.71.0 x402-stacks@2.0.3 axios
//   node ~/Projects/agentpay/tools/stacks_client_interop.mjs
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const BASE = process.env.AGENTPAY_URL || "https://agentpay.tools";
const EXPECTED = process.env.EXPECTED_ADDRESS || "SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5";
const SBTC_ADDR = "SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4";
const SBTC = `${SBTC_ADDR}.sbtc-token::sbtc-token`;
const HIRO = "https://api.hiro.so";
const FUND_SATS = 50n;
const FUND_USTX = 70000n;  // AIBTC fee can hit its 0.05 STX cap; the rest pays the sBTC sweep
const CALL = ["/tools/pre_trade_check/call", { parameters: { symbol: "BTC", side: "long", size_usd: 1000 } }];

// Read a line with no echo: raw mode, so it works on every Node version
// (readline's _writeToOutput override silently stopped working on 20+).
function askHidden(q) {
  return new Promise((resolve) => {
    const { stdin, stdout } = process;
    stdout.write(q);
    let buf = "";
    const wasRaw = stdin.isRaw;
    stdin.setRawMode(true); stdin.resume(); stdin.setEncoding("utf8");
    const done = (v) => { stdin.setRawMode(wasRaw ?? false); stdin.pause(); stdin.off("data", onData); stdout.write("\n"); resolve(v); };
    const onData = (ch) => {
      for (const c of ch) {
        if (c === "\u0003") { stdout.write("\n"); process.exit(130); }
        if (c === "\r" || c === "\n") return done(buf.trim());
        if (c === "\u007f" || c === "\b") buf = buf.slice(0, -1);
        else if (c >= " ") buf += c;
      }
    };
    stdin.on("data", onData);
  });
}

function stop(msg) { console.error(`\nStopped: ${msg}`); process.exit(1); }

// The flagship pays from this wallet daily at 13:00 UTC; overlapping would clash on the nonce.
const now = new Date();
const minutes = now.getUTCHours() * 60 + now.getUTCMinutes();
if (!process.env.SKIP_GUARDS && minutes >= 12 * 60 + 30 && minutes <= 13 * 60 + 30)
  stop("too close to the flagship's 13:00 UTC run. Try again after 13:30 UTC.");

process.env.NETWORK = "mainnet";
const aibtc = await import(pathToFileURL(path.resolve("node_modules/@aibtc/mcp-server/dist/services/x402.service.js")).href);
const req = createRequire(path.join(process.cwd(), "noop.js"));  // resolve from the run folder, not this file's
const x402stacks = req("x402-stacks");
const axios = req("axios");
const wsdk = req("@stacks/wallet-sdk");
const tx = req("@stacks/transactions");

async function balances(addr) {
  const bal = (await axios.get(`${HIRO}/extended/v1/address/${addr}/balances`)).data;
  return { sats: BigInt(bal.fungible_tokens?.[SBTC]?.balance || 0), ustx: BigInt(bal.stx?.balance || 0) };
}

async function waitTx(txid, label) {
  const id = txid.startsWith("0x") ? txid : `0x${txid}`;
  process.stdout.write(`  waiting for ${label} to confirm `);
  for (let i = 0; i < 60; i++) {
    const r = await axios.get(`${HIRO}/extended/v1/tx/${id}`, { validateStatus: () => true });
    const st = r.data?.tx_status;
    if (st === "success") { process.stdout.write("ok\n"); return; }
    if (st && st !== "pending") { process.stdout.write("\n"); stop(`${label} ${id} ended as ${st}.`); }
    process.stdout.write(".");
    await new Promise((r) => setTimeout(r, 5000));
  }
  stop(`${label} ${id} not confirmed after 5 minutes. Check the explorer before rerunning.`);
}

async function send(t, label) {
  const r = await tx.broadcastTransaction({ transaction: t, network: "mainnet" });
  if (!r.txid || r.error) stop(`${label} broadcast failed: ${r.error || ""} ${r.reason || ""}`);
  console.log(`  ${label}: 0x${r.txid}`);
  return `0x${r.txid}`;
}

function sbtcTransfer(key, from, to, amount, nonce) {
  return tx.makeContractCall({
    contractAddress: SBTC_ADDR, contractName: "sbtc-token", functionName: "transfer",
    functionArgs: [tx.Cl.uint(amount), tx.Cl.principal(from), tx.Cl.principal(to), tx.Cl.none()],
    postConditionMode: "deny",
    postConditions: [tx.Pc.principal(from).willSendEq(amount).ft(`${SBTC_ADDR}.sbtc-token`, "sbtc-token")],
    senderKey: key, network: "mainnet", fee: 10000n, nonce,
  });
}

let words = await askHidden("Demo payer Secret Key (24 words, hidden): ");
let wallet = await wsdk.generateWallet({ secretKey: words, password: "" });
words = "";
let payer;
for (let i = 0; i < 20 && !payer; i++) {
  if (i >= wallet.accounts.length) wallet = wsdk.generateNewAccount(wallet);
  if (wsdk.getStxAddress(wallet.accounts[i], "mainnet") === EXPECTED) payer = wallet.accounts[i];
}
if (!payer) stop(`${EXPECTED} is not among the first 20 accounts of this Secret Key. Nothing was paid.`);
const payerKey = payer.stxPrivateKey;
wallet = null;

const throwawayWords = wsdk.generateSecretKey(256);
const throwaway = { words: throwawayWords, ...(await aibtc.mnemonicToAccount(throwawayWords, "mainnet")) };
throwaway.key = throwaway.privateKey;
console.log(`Demo payer ${EXPECTED} (found on the Secret Key)\nThrowaway for AIBTC ${throwaway.address}`);

if (!process.env.SKIP_GUARDS) {
  const b = await balances(EXPECTED);
  console.log(`Demo payer balance: ${b.sats} sats sBTC, ${Number(b.ustx) / 1e6} STX`);
  if (b.sats < 100n || b.ustx < 200000n) stop("needs at least 100 sats sBTC and 0.2 STX.");
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
  const account = x402stacks.privateKeyToAccount(payerKey, "mainnet");
  return x402stacks.wrapAxiosWithPayment(axios.create({ baseURL: BASE, timeout: 180000 }), account).post(...CALL);
});
if (results[0].ok) await waitTx(results[0].txid, "x402-stacks payment");

console.log(`\nFunding the throwaway with ${FUND_SATS} sats sBTC and ${Number(FUND_USTX) / 1e6} STX …`);
const nonce = BigInt((await axios.get(`${HIRO}/extended/v1/address/${EXPECTED}/nonces`)).data.possible_next_nonce);
const funding = [
  await send(await sbtcTransfer(payerKey, EXPECTED, throwaway.address, FUND_SATS, nonce), "sBTC"),
  await send(await tx.makeSTXTokenTransfer({ recipient: throwaway.address, amount: FUND_USTX,
    senderKey: payerKey, network: "mainnet", fee: 2000n, nonce: nonce + 1n }), "STX"),
];
for (const id of funding) await waitTx(id, "funding");

process.env.CLIENT_MNEMONIC = throwaway.words;
await run("@aibtc/mcp-server", "1.71.0", async () => (await aibtc.createApiClient(BASE)).post(...CALL));
delete process.env.CLIENT_MNEMONIC;
throwaway.words = "";
if (results[1].ok) await waitTx(results[1].txid, "AIBTC payment");

// Best effort: a failed sweep leaves a few cents on the throwaway, nothing else.
const sweep = [];
try {
  console.log("\nSweeping the throwaway back to the demo payer …");
  const b = await balances(throwaway.address);
  let n = BigInt((await axios.get(`${HIRO}/extended/v1/address/${throwaway.address}/nonces`)).data.possible_next_nonce);
  let ustx = b.ustx;
  if (b.sats > 0n && ustx >= 10000n) {
    sweep.push(await send(await sbtcTransfer(throwaway.key, throwaway.address, EXPECTED, b.sats, n++), "sBTC back"));
    ustx -= 10000n;
  }
  if (ustx > 2000n)
    sweep.push(await send(await tx.makeSTXTokenTransfer({ recipient: EXPECTED, amount: ustx - 2000n,
      senderKey: throwaway.key, network: "mainnet", fee: 2000n, nonce: n }), "STX back"));
} catch (e) {
  console.log(`  sweep failed: ${e.message}`);
}

const out = path.join(os.homedir(), "Projects/agentpay/notes",
                      `stacks_standard_clients_mainnet_${now.toISOString().slice(0, 10)}.json`);
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, JSON.stringify({ gateway: BASE, payer: EXPECTED, aibtc_payer: throwaway.address,
  at: now.toISOString(), results, funding, sweep }, null, 2));
console.log(`\nEvidence saved to ${out}`);
process.exit(results.every((r) => r.ok) ? 0 : 1);
