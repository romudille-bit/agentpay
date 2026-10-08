// Pays the targets on Arc mainnet and records what a buyer gets: price, delivery, latency, proof.
// `--dry` runs only the unpaid pass (no key needed). `--only a,b` limits targets.
import path from "node:path";
import { attest, clients } from "./anchor.mjs";
import { matchTargets, pullCatalog, summarize } from "./catalog.mjs";
import { ARC, DOCS, ROOT, addressUrl, arcOptions, decode402, domainOf, now, readJson, timedFetch, txUrl, usd, writeJson } from "./lib.mjs";
import { makeExact, makeGateway, payBatched, payExact } from "./pay.mjs";

const args = process.argv.slice(2);
const DRY = args.includes("--dry");
const ONLY = args.includes("--only") ? args[args.indexOf("--only") + 1].split(",").filter(Boolean) : [];
const isTx = (s) => /^0x[0-9a-f]{64}$/i.test(s ?? "");

async function unpaid(t) {
  const init = { method: t.method, headers: { accept: "application/json" } };
  if (t.body !== undefined) { init.body = JSON.stringify(t.body); init.headers["content-type"] = "application/json"; }
  try {
    const { res, ms } = await timedFetch(t.resource, init);
    const pr = decode402(res.headers.get("payment-required"));
    const opts = arcOptions(pr?.accepts);
    const mine = opts.find((o) => domainOf(o) === t.domain);
    return {
      status: res.status, ms,
      header402: Boolean(pr),
      arcOptions: opts.map((o) => ({ domain: domainOf(o), usd: usd(o.amount), payTo: o.payTo })),
      challenged: mine?.amount ?? null,
      challengedUsd: mine ? usd(mine.amount) : null,
      validatedFirst: res.status !== 402, // provider rejected or served the request before asking for payment
    };
  } catch (e) {
    return { status: null, ms: null, header402: false, arcOptions: [], challenged: null, challengedUsd: null, validatedFirst: false, error: e.message };
  }
}

function proofOf(domain, settle) {
  const tx = settle?.transaction;
  if (isTx(tx)) return `${domain === "batched" ? "gateway" : "eip3009"}:tx:${tx}`;
  if (tx) return `gateway:transfer:${tx}`;
  return "none";
}

const items = await pullCatalog();
const catalog = summarize(items);
await writeJson(path.join(DOCS, "catalog.json"), catalog);
let targets = matchTargets(items, await readJson(path.join(ROOT, "targets.json"), []));
if (ONLY.length) targets = targets.filter((t) => ONLY.includes(t.id));

const key = process.env.ARC_PRIVATE_KEY;
if (!DRY && !key) { console.error("ARC_PRIVATE_KEY missing (use --dry for the unpaid pass)"); process.exit(1); }
const maxSpend = BigInt(Math.round(Number(process.env.ARC_MAX_SPEND ?? "1") * 1e6));
const anchorAddress = process.env.ARC_ANCHOR_ADDRESS || null;

let gateway, exact, chain;
if (!DRY) {
  gateway = makeGateway(key);
  exact = makeExact(key, process.env.ARC_MAX_SPEND ?? "1");
  chain = clients(key);
  const b = await gateway.client.getBalances();
  console.log(`payer ${gateway.address}  wallet ${b.wallet.formatted} USDC  gateway ${b.gateway.formattedAvailable} USDC`);
  const needBatched = targets.filter((t) => t.domain === "batched").reduce((s, t) => s + BigInt(Math.round(Number(t.max) * 1e6)), 0n);
  const amt = process.env.ARC_GATEWAY_DEPOSIT ?? "0.25";
  if (b.gateway.available < needBatched && Number(amt) > 0) {
    console.log(`gateway balance short of ${usd(needBatched)}; depositing ${amt} USDC`);
    const d = await gateway.client.deposit(amt);
    console.log(`deposit ${txUrl(d.depositTxHash)}`);
  }
}

const rows = [];
let spent = 0n;
for (const t of targets) {
  process.stdout.write(`${t.id} … `);
  const row = { id: t.id, provider: t.provider, resource: t.resource, method: t.method, domain: t.domain, note: t.note ?? null, listed: t.listed, unpaid: await unpaid(t), paid: null, proof: null, attestation: null };
  if (!DRY) {
    if (spent + BigInt(Math.round(Number(t.max) * 1e6)) > maxSpend) {
      row.paid = { error: `skipped: run cap ${usd(maxSpend)} would be exceeded` };
    } else {
      row.paid = t.domain === "batched" ? await payBatched(gateway, t) : await payExact(exact, t);
      row.paid.chargedUsd = row.paid.charged ? usd(row.paid.charged) : null;
      row.paid.receiptReturned = row.paid.settle != null;
      if (row.paid.settle?.success !== false && row.paid.charged && (row.paid.delivered || row.paid.settle?.success)) spent += BigInt(row.paid.charged);
      row.proof = proofOf(t.domain, row.paid.settle);
      row.priceMatch = row.listed && row.paid.charged ? row.listed.amount === row.paid.charged : null;
      if (anchorAddress && (row.paid.charged || row.paid.delivered)) {
        try { row.attestation = await attest(chain, anchorAddress, row); } catch (e) { row.attestation = { error: e.message }; }
      }
    }
  }
  rows.push(row);
  const u = row.unpaid, p = row.paid;
  console.log(`unpaid ${u.status} ${u.ms}ms challenged=${u.challengedUsd ?? "-"}` + (p ? ` | paid ${p.status ?? "-"} ${p.ms ?? "-"}ms charged=${p.chargedUsd ?? "-"} delivered=${p.delivered} proof=${row.proof}${p.error ? ` err=${p.error}` : ""}` : ""));
}

const summary = {
  runAt: now(), network: ARC, dry: DRY,
  payer: DRY ? null : gateway.address, payerUrl: DRY ? null : addressUrl(gateway.address),
  anchor: anchorAddress, anchorUrl: anchorAddress ? addressUrl(anchorAddress) : null,
  targets: rows.length, providers: new Set(rows.map((r) => r.provider)).size,
  answered402: rows.filter((r) => r.unpaid.header402).length,
  validatedFirst: rows.filter((r) => r.unpaid.validatedFirst).length,
  paid: rows.filter((r) => r.paid?.charged).length,
  delivered: rows.filter((r) => r.paid?.delivered).length,
  deliveredWithoutReceipt: rows.filter((r) => r.paid?.delivered && !r.paid.receiptReturned).length,
  priceMismatch: rows.filter((r) => r.priceMatch === false).length,
  attested: rows.filter((r) => r.attestation?.txHash).length,
  spentUsd: usd(spent),
  proofTypes: Object.fromEntries(["eip3009:tx", "gateway:tx", "gateway:transfer", "none"].map((k) => [k, rows.filter((r) => (r.proof ?? "none").startsWith(k)).length])),
};
const ledger = { summary, catalog: { pulledAt: catalog.pulledAt, listings: catalog.listings, providers: catalog.providers.length, domains: catalog.domains }, rows };
await writeJson(path.join(DOCS, "ledger.json"), ledger);
if (!DRY) await writeJson(path.join(DOCS, "runs", `${summary.runAt.replace(/[:.]/g, "-")}.json`), ledger);
console.log(JSON.stringify(summary, null, 2));
