// Mainnet proof of the server-side session cap with a standard Stacks x402
// client (x402-stacks, no AgentPay SDK): open a $0.02 session, pay
// pre_trade_check twice under it, get the third call refused with nothing
// broadcast, then read GET /v1/session/{id}. Evidence goes to notes/.
// Run from a folder with the client installed (same as stacks_client_interop.mjs):
//   npm i x402-stacks@2.0.3 axios @stacks/wallet-sdk
//   node ~/Projects/agentpay/tools/session_cap_proof.mjs
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { createRequire } from "node:module";

const BASE = process.env.AGENTPAY_URL || "https://agentpay.tools";
const EXPECTED = process.env.EXPECTED_ADDRESS || "SP27VCS0HWCMKEZE8ESRG8J95RN3BXX559KPNBWK5";
const HIRO = "https://api.hiro.so";
const MAX_SPEND = process.env.MAX_SPEND || "0.02";
const CALL = ["/tools/pre_trade_check/call", { parameters: { symbol: "BTC", side: "long", size_usd: 1000 } }];

const req = createRequire(path.join(process.cwd(), "noop.js"));
const x402stacks = req("x402-stacks");
const axios = req("axios");
const wsdk = req("@stacks/wallet-sdk");

function askHidden(q) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    rl._writeToOutput = (s) => { if (s.includes(q)) rl.output.write(s); };
    rl.question(q, (a) => { rl.close(); process.stdout.write("\n"); resolve(a.trim()); });
  });
}
function stop(msg) { console.error(`\nStopped: ${msg}`); process.exit(1); }

const minutes = new Date().getUTCHours() * 60 + new Date().getUTCMinutes();
if (!process.env.SKIP_GUARDS && minutes >= 12 * 60 + 30 && minutes <= 13 * 60 + 30)
  stop("too close to the flagship's 13:00 UTC run. Try again after 13:30 UTC.");

let words = await askHidden("Demo payer Secret Key (24 words, hidden): ");
let wallet = await wsdk.generateWallet({ secretKey: words, password: "" });
words = "";
let payer;
for (let i = 0; i < 20 && !payer; i++) {
  if (i >= wallet.accounts.length) wallet = wsdk.generateNewAccount(wallet);
  if (wsdk.getStxAddress(wallet.accounts[i], "mainnet") === EXPECTED) payer = wallet.accounts[i];
}
if (!payer) stop(`${EXPECTED} is not among the first 20 accounts of this Secret Key. Nothing was paid.`);
const account = x402stacks.privateKeyToAccount(payer.stxPrivateKey, "mainnet");
wallet = null;

const http = x402stacks.wrapAxiosWithPayment(axios.create({ baseURL: BASE, timeout: 180000 }), account);
const nonceOf = async () => (await axios.get(`${HIRO}/extended/v1/address/${EXPECTED}/nonces`)).data.possible_next_nonce;

const evidence = { gateway: BASE, payer: EXPECTED, client: "x402-stacks 2.0.3", at: new Date().toISOString(), steps: [] };
const txOf = (res) => { const h = res.data?.payment?.tx_hash || ""; return h.startsWith("0x") ? h : `0x${h}`; };

console.log(`\n→ opening a session with max_spend ${MAX_SPEND} …`);
const created = (await http.post("/tools/session_create/call", { parameters: { max_spend: MAX_SPEND, label: "cap proof" } })).data;
const s = created.result || {};
if (!s.enforced) stop(`gateway did not enforce this session (enforced=${s.enforced}). Is SESSION_ENFORCEMENT on?`);
console.log(`  session ${s.session_id} (reused=${s.reused}) tx ${txOf({ data: created })}`);
evidence.steps.push({ step: "session_create", session_id: s.session_id, reused: s.reused, tx: txOf({ data: created }), max_spend: MAX_SPEND });

for (const n of [1, 2]) {
  console.log(`\n→ paid call ${n} under the cap …`);
  const res = await http.post(...CALL);
  const sess = res.data.session || {};
  console.log(`  ok  tx ${txOf(res)}  spent ${sess.spent}/${sess.max_spend} remaining ${sess.remaining}`);
  evidence.steps.push({ step: `call_${n}`, tx: txOf(res), session: sess });
}

console.log(`\n→ call 3, past the cap …`);
const nonceBefore = await nonceOf();
try {
  const res = await http.post(...CALL);
  stop(`call 3 was served (tx ${txOf(res)}) — the cap did not hold.`);
} catch (e) {
  const body = e.response?.data || {};
  const nonceAfter = await nonceOf();
  const refused = e.response?.status === 402 && body.error_reason === "session_cap_exceeded";
  console.log(`  ${refused ? "refused" : "UNEXPECTED"} HTTP ${e.response?.status} ${JSON.stringify(body).slice(0, 300)}`);
  console.log(`  payer nonce before ${nonceBefore} after ${nonceAfter} (${nonceAfter === nonceBefore ? "nothing broadcast" : "SOMETHING WAS BROADCAST"})`);
  evidence.steps.push({ step: "call_3_refused", status: e.response?.status, body, nonce_before: nonceBefore, nonce_after: nonceAfter });
  if (!refused || nonceAfter !== nonceBefore) stop("the refusal did not look right; read the body above.");
}

const view = (await axios.get(`${BASE}/v1/session/${s.session_id}`)).data;
console.log(`\n→ GET /v1/session/${s.session_id}\n  status=${view.status} spent=${view.spent}/${view.max_spend} receipts=${view.receipts.length}`);
evidence.session_view = view;

fs.mkdirSync(path.join(process.env.HOME, "Projects/agentpay/notes"), { recursive: true });
const out = path.join(process.env.HOME, "Projects/agentpay/notes", `session_cap_proof_${new Date().toISOString().slice(0, 10)}.json`);
fs.writeFileSync(out, JSON.stringify(evidence, null, 2));
console.log(`\nEvidence written to ${out}`);
