// Lists AgentPay's priced tools on the stx402.com directory. The demo payer
// pays the registration fee in STX; the entries are owned by the gateway
// payee, so edits and deletes need a SIP-018 signature from that Leather account.
// Run from the folder used for stacks_client_interop.mjs:
//   node ~/Projects/agentpay/tools/stx402_register.mjs
import readline from "node:readline";
import path from "node:path";
import { createRequire } from "node:module";

const DIRECTORY = "https://stx402.com";
const PAYER = "SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5";
const OWNER = "SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C";
const ENTRIES = [
  {
    url: "https://agentpay.tools/tools/pre_trade_check/call",
    name: "AgentPay pre_trade_check",
    description: "One call before a trade: live orderbook slippage at your size, cross-exchange " +
      "funding, open-interest crowding and contract security, combined into an ok/caution/avoid " +
      "verdict with a per-factor breakdown. $0.01, payable in sBTC.",
    category: "analytics",
    tags: ["trading", "pre-trade", "risk", "sbtc", "agentpay"],
  },
  {
    url: "https://agentpay.tools/tools/verified_route/call",
    name: "AgentPay verified_route",
    description: "Buyer-side trust oracle for x402: sweeps the marketplace, collapses sybil " +
      "listing clusters, ranks tools by real unique payers and returns one vetted recommendation. " +
      "$0.01, payable in sBTC. Recommended sellers are on Base and Solana for now.",
    category: "analytics",
    tags: ["x402", "discovery", "trust", "sbtc", "agentpay"],
  },
  {
    url: "https://agentpay.tools/tools/session_create/call",
    name: "AgentPay session_create",
    description: "Open a spend cap for your wallet: every priced AgentPay call from that address " +
      "is reserved against max_spend before it settles and refused past it with nothing charged. " +
      "$0.01, payable in sBTC; enforced on Stacks and Base. Read it back at /v1/session/{id}.",
    category: "infrastructure",
    tags: ["x402", "session", "spend-cap", "sbtc", "agentpay"],
  },
];

function ask(q) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(q, (a) => { rl.close(); resolve(a.trim()); });
  });
}
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

// The flagship pays from the same wallet at 13:00 UTC; overlapping would clash on the nonce.
const m = new Date().getUTCHours() * 60 + new Date().getUTCMinutes();
if (m >= 12 * 60 + 30 && m <= 13 * 60 + 30) stop("too close to the flagship's 13:00 UTC run.");

const req = createRequire(path.join(process.cwd(), "noop.js"));  // resolve from the run folder
const axios = req("axios");
const wsdk = req("@stacks/wallet-sdk");
const x402stacks = req("x402-stacks");

const listed = new Set((await axios.get(`${DIRECTORY}/registry/list`)).data.entries.map((e) => e.url));
const todo = ENTRIES.filter((e) => !listed.has(e.url));
if (!todo.length) stop("every entry is already listed.");
console.log(`Owner ${OWNER}, fee paid by ${PAYER}. To register:`);
for (const e of todo) console.log(`  ${e.name}  ${e.url}`);
if ((await ask("Proceed? [y/N] ")).toLowerCase() !== "y") stop("nothing registered.");

let words = await askHidden("Demo payer Secret Key (24 words, hidden): ");
let wallet = await wsdk.generateWallet({ secretKey: words, password: "" });
words = "";
let payer;
for (let i = 0; i < 20 && !payer; i++) {
  if (i >= wallet.accounts.length) wallet = wsdk.generateNewAccount(wallet);
  if (wsdk.getStxAddress(wallet.accounts[i], "mainnet") === PAYER) payer = wallet.accounts[i];
}
wallet = null;
if (!payer) stop(`${PAYER} is not among the first 20 accounts of this Secret Key.`);

const client = x402stacks.wrapAxiosWithPayment(
  axios.create({ baseURL: DIRECTORY, timeout: 180000 }),
  x402stacks.privateKeyToAccount(payer.stxPrivateKey, "mainnet"));

let failed = 0;
for (const e of todo) {
  process.stdout.write(`\n→ ${e.name} … `);
  try {
    const r = (await client.post("/registry/register", { ...e, owner: OWNER })).data;
    console.log(`ok  id=${r.entry?.id} status=${r.entry?.status}`);
    if (r.probeResult) console.log(`   probe: ${JSON.stringify(r.probeResult).slice(0, 300)}`);
  } catch (err) {
    failed++;
    console.log(`FAILED ${err.response?.status || ""} ${err.message}`);
    if (err.response?.data) console.log(`   ${JSON.stringify(err.response.data).slice(0, 300)}`);
  }
}
process.exit(failed ? 1 : 0);
