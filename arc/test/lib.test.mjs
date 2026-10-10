import { test } from "node:test";
import assert from "node:assert/strict";
import { arcOptions, decode402, domainOf, usd } from "../src/lib.mjs";

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64");

test("decode402 reads the base64 PAYMENT-REQUIRED header and tolerates garbage", () => {
  const pr = { x402Version: 2, accepts: [{ scheme: "exact", network: "eip155:5042", amount: "1000" }] };
  assert.deepEqual(decode402(b64(pr)), pr);
  assert.equal(decode402("not base64 json"), null);
  assert.equal(decode402(null), null);
});

test("arcOptions keeps only Arc entries and domainOf tells Gateway from EIP-3009", () => {
  const accepts = [
    { network: "eip155:8453", extra: { name: "USD Coin", version: "2" } },
    { network: "eip155:5042", extra: { name: "GatewayWalletBatched", version: "1" } },
    { network: "eip155:5042", extra: { name: "USDC", version: "2" } },
  ];
  const arc = arcOptions(accepts);
  assert.equal(arc.length, 2);
  assert.deepEqual(arc.map(domainOf), ["batched", "eip3009"]);
  assert.equal(domainOf(undefined), "eip3009");
});

test("usd formats atomic USDC without trailing zeros", () => {
  assert.equal(usd("1000"), "0.001");
  assert.equal(usd(100000), "0.1");
  assert.equal(usd("0"), "0");
  assert.equal(usd(1500000), "1.5");
});
