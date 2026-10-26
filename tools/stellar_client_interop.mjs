// Pays pre_trade_check with the stock @x402/stellar client — no AgentPay code on
// the client side — and saves the settlement as evidence. Run from a folder with
// the client installed:
//   npm i @x402/fetch@2.28.0 @x402/stellar@2.28.0
//   STELLAR_PAYER_SECRET=S... node ~/Projects/agentpay/tools/stellar_client_interop.mjs
// AGENTPAY_URL (default https://agentpay.tools), STELLAR_NETWORK_ID
// (stellar:testnet | stellar:pubnet), STELLAR_RPC_URL (required on pubnet),
// OUT (evidence path). The secret is never printed or saved.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

// Resolve the clients from the run folder, not from this file's.
const mod = (name) => import(pathToFileURL(createRequire(path.join(process.cwd(), "noop.js")).resolve(name)).href);
const { wrapFetchWithPaymentFromConfig } = await mod("@x402/fetch");
const { createEd25519Signer } = await mod("@x402/stellar");
const { ExactStellarScheme } = await mod("@x402/stellar/exact/client");

const BASE = process.env.AGENTPAY_URL || "https://agentpay.tools";
const NETWORK = process.env.STELLAR_NETWORK_ID || "stellar:testnet";
const SECRET = process.env.STELLAR_PAYER_SECRET;
const OUT = process.env.OUT || `stellar_standard_client_${NETWORK.split(":")[1]}_${new Date().toISOString().slice(0, 10)}.json`;
const CALL = ["/tools/pre_trade_check/call", { parameters: { symbol: "BTC", side: "long", size_usd: 1000 } }];

if (!SECRET) { console.error("STELLAR_PAYER_SECRET is required"); process.exit(2); }
const signer = createEd25519Signer(SECRET, NETWORK);
const rpcConfig = process.env.STELLAR_RPC_URL ? { rpcUrl: process.env.STELLAR_RPC_URL } : undefined;
const fetchWithPayment = wrapFetchWithPaymentFromConfig(fetch, {
  schemes: [{ network: NETWORK, client: new ExactStellarScheme(signer, rpcConfig) }],
});

// MODE=replay: build one signed payload by hand and present it twice; the
// second attempt must be refused (the auth entry is single-use on chain).
if (process.env.MODE === "replay") {
  const url = `${BASE}${CALL[0]}`;
  const init = { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(CALL[1]) };
  const first = await fetch(url, init);
  const req = JSON.parse(Buffer.from(first.headers.get("payment-required"), "base64").toString("utf8"));
  const accepted = req.accepts.find((a) => a.network === NETWORK);
  const scheme = new ExactStellarScheme(signer, rpcConfig);
  const { payload } = await scheme.createPaymentPayload(2, accepted);
  const sig = Buffer.from(JSON.stringify({ x402Version: 2, resource: req.resource, accepted, payload })).toString("base64");
  const results = [];
  for (const n of [1, 2]) {
    const r = await fetch(url, { ...init, headers: { ...init.headers, "payment-signature": sig } });
    const b = await r.json().catch(() => ({}));
    results.push({ attempt: n, status: r.status, payment_status: b.payment_status, error_reason: b.error_reason, tx_hash: (b.receipt || b.payment)?.tx_hash });
    console.log(`attempt ${n}: ${r.status} ${b.payment_status || "ok"} ${b.error_reason || (b.receipt || b.payment)?.tx_hash || ""}`);
  }
  fs.writeFileSync(OUT, JSON.stringify({ ran_at: new Date().toISOString(), mode: "replay", network: NETWORK,
    payer: signer.address, results }, null, 2));
  process.exit(results[0].status === 200 && results[1].status !== 200 ? 0 : 1);
}

const t0 = Date.now();
const res = await fetchWithPayment(`${BASE}${CALL[0]}`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(CALL[1]),
});
const elapsedMs = Date.now() - t0;
const text = await res.text();
let body; try { body = JSON.parse(text); } catch { body = text; }

const prHeader = res.headers.get("payment-response");
let paymentResponse = null;
if (prHeader) {
  try { paymentResponse = JSON.parse(Buffer.from(prHeader, "base64").toString("utf8")); }
  catch { paymentResponse = prHeader; }
}

const receipt = body && typeof body === "object" ? (body.receipt || body.payment || null) : null;
const evidence = {
  ran_at: new Date().toISOString(),
  client: "@x402/fetch + @x402/stellar 2.28.0 (stock)",
  gateway: BASE,
  network: NETWORK,
  payer: signer.address,
  call: CALL[0],
  status: res.status,
  elapsed_ms: elapsedMs,
  payment_response: paymentResponse,
  receipt,
  tx_hash: paymentResponse?.transaction || receipt?.tx_hash || null,
};
fs.writeFileSync(OUT, JSON.stringify(evidence, null, 2));

console.log(`status ${res.status} in ${elapsedMs} ms`);
console.log(`payer  ${signer.address}`);
console.log(`tx     ${evidence.tx_hash || "(none)"}`);
if (res.status !== 200) console.log(typeof body === "string" ? body.slice(0, 400) : JSON.stringify(body).slice(0, 400));
console.log(`saved  ${OUT}`);
process.exit(res.status === 200 ? 0 : 1);
