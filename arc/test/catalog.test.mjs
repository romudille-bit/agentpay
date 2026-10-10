import { test } from "node:test";
import assert from "node:assert/strict";
import { matchTargets, summarize } from "../src/catalog.mjs";

const arc = (amount, name = "GatewayWalletBatched", payTo = "0xpay") => ({ scheme: "exact", network: "eip155:5042", amount, payTo, extra: { name, version: "1" } });
const base = (amount) => ({ scheme: "exact", network: "eip155:8453", amount, payTo: "0xbase", extra: { name: "USD Coin", version: "2" } });
const items = [
  { resource: "https://a.example/one", lastUpdated: "2026-10-01", accepts: [arc("100000"), base("100000")], metadata: { provider: { name: "A" }, siwx: false } },
  { resource: "https://a.example/two", lastUpdated: "2026-10-01", accepts: [arc("5000")], metadata: { provider: { name: "A" }, siwx: false } },
  { resource: "https://b.example/x", lastUpdated: "2026-10-02", accepts: [arc("1000", "USDC"), arc("1000")], metadata: { provider: { name: "B" }, siwx: true } },
];

test("summarize counts providers, domains, arc-only listings and prices", () => {
  const s = summarize(items);
  assert.equal(s.listings, 3);
  assert.equal(s.arcOnly, 2);
  assert.deepEqual(s.domains, { batched: 3, eip3009: 1 });
  assert.equal(s.priceUsd.atOrUnder1c, 2);
  assert.deepEqual(s.providers.map((p) => [p.name, p.listings, p.batched, p.eip3009]), [["A", 2, 2, 0], ["B", 1, 1, 1]]);
  assert.equal(s.providers[1].minUsd, "0.001");
});

test("matchTargets resolves a target to its listing by URL (ignoring the query) and domain", () => {
  const targets = [
    { id: "b-eip", resource: "https://b.example/x?q=1", domain: "eip3009" },
    { id: "b-gw", resource: "https://b.example/x", domain: "batched" },
    { id: "missing", resource: "https://nope.example/", domain: "batched" },
  ];
  const [eip, gw, missing] = matchTargets(items, targets);
  assert.equal(eip.listed.usd, "0.001");
  assert.equal(eip.listed.siwx, true);
  assert.equal(gw.listed.amount, "1000");
  assert.equal(missing.listed, null);
});
