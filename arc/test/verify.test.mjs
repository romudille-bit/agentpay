import { test } from "node:test";
import assert from "node:assert/strict";
import { reconcile } from "../src/verify.mjs";

const row = (id, domain, paid) => ({ id, domain, resource: `https://${id}`, listed: { payTo: "0xPAYTO" }, paid: { charged: "5000", receiptReturned: false, settle: null, ...paid }, proof: "none" });

test("reconcile fills settled from the Gateway ledger only inside the row's time window", () => {
  const now = Date.parse("2026-10-10T17:00:00Z");
  const rows = [
    row("fresh", "batched", { paidAt: new Date(now).toISOString() }),
    row("old", "batched", { paidAt: new Date(now).toISOString() }),
  ];
  const gw = [
    { to: "0xpayto", amount: "5000", id: "t-earlier", status: "completed", createdAt: new Date(now - 10 * 60_000).toISOString() },
    { to: "0xpayto", amount: "5000", id: "t-now", status: "received", createdAt: new Date(now + 5_000).toISOString() },
  ];
  reconcile(rows, [], gw);
  assert.equal(rows[0].paid.settled.id, "t-now");
  assert.equal(rows[0].proof, "gateway:transfer:t-now");
  assert.equal(rows[1].paid.settled, null, "the second row must not reuse the same transfer or grab an old one");
});

test("reconcile matches EIP-3009 rows to an on-chain transfer after the call's block", () => {
  const rows = [row("chain", "eip3009", { blockBefore: 100 })];
  const chain = [
    { to: "0xpayto", amount: "5000", txHash: "0xold", block: 50 },
    { to: "0xpayto", amount: "5000", txHash: "0xnew", block: 103 },
  ];
  reconcile(rows, chain, []);
  assert.deepEqual(rows[0].paid.settled, { via: "onchain", txHash: "0xnew", block: 103 });
  assert.equal(rows[0].proof, "eip3009:tx:0xnew");
});

test("reconcile leaves rows that already carry a receipt alone", () => {
  const rows = [row("rcpt", "eip3009", { settle: { success: true, transaction: "0xabc" }, receiptReturned: true })];
  reconcile(rows, [{ to: "0xpayto", amount: "5000", txHash: "0xzzz", block: 1 }], []);
  assert.equal(rows[0].paid.settled, undefined);
  assert.equal(rows[0].proof, "none");
});
