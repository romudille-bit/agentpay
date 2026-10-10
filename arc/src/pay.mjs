// Two buyer paths on Arc: Circle Gateway batched (GatewayClient) and plain EIP-3009 (x402 core + evm).
import { GatewayClient } from "@circle-fin/x402-batching/client";
import { x402Client } from "@x402/core/client";
import { decodePaymentResponseHeader } from "@x402/core/http";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment } from "@x402/fetch";
import { privateKeyToAccount } from "viem/accounts";
import { ARC, BATCHED, USDC, sha256 } from "./lib.mjs";

const bodyInit = (t) => (t.body === undefined ? {} : { body: JSON.stringify(t.body), headers: { "content-type": "application/json" } });
const emptyResult = () => ({ paidAt: new Date().toISOString(), status: null, ms: null, charged: null, settle: null, delivered: false, bodyBytes: 0, bodySha256: null, errorBody: null, error: null });
const atomic = (usd) => BigInt(Math.round(Number(usd) * 1e6));

export function deliveredFrom(status, text) {
  if (status < 200 || status >= 300) return false;
  if (!text || !text.trim()) return false;
  try { const j = JSON.parse(text); return !(j && typeof j === "object" && "error" in j && Object.keys(j).length <= 2); } catch { return true; }
}

// Hooks are registered once; `slot` points at the result object of the call in flight.
function capHook(payer) {
  return async (ctx) => {
    payer.slot.charged = ctx.selectedRequirements.amount;
    if (BigInt(ctx.selectedRequirements.amount) > payer.max) return { abort: true, reason: `challenge ${ctx.selectedRequirements.amount} > max ${payer.max}` };
  };
}

export function makeGateway(privateKey) {
  const payer = { slot: emptyResult(), max: 0n };
  payer.client = new GatewayClient({ chain: "arc", privateKey })
    .onBeforePaymentCreation(capHook(payer))
    .onPaymentResponse(async (ctx) => {
      payer.slot.settle = ctx.settleResponse ?? null;
      if (ctx.error) payer.slot.error = ctx.error.message;
    });
  payer.address = payer.client.address;
  return payer;
}

export async function payBatched(payer, t) {
  const out = (payer.slot = emptyResult());
  payer.max = atomic(t.max);
  const t0 = performance.now();
  try {
    const r = await payer.client.pay(t.resource, { method: t.method, body: t.body });
    out.status = r.status;
    const text = JSON.stringify(r.data ?? "");
    out.bodyBytes = Buffer.byteLength(text);
    out.bodySha256 = sha256(text);
    out.delivered = deliveredFrom(r.status, text);
    if (!out.charged && r.amount) out.charged = r.amount.toString();
  } catch (e) {
    out.error = /^Payment failed: ./.test(e.message) ? e.message : (out.error ?? e.message);
    const m = /status (\d{3})/.exec(e.message);
    if (m) out.status = Number(m[1]);
  }
  out.ms = Math.round(performance.now() - t0);
  return out;
}

export function makeExact(privateKey, maxUsd) {
  const account = privateKeyToAccount(privateKey);
  const pickArc = (_v, reqs) => {
    const r = reqs.find((q) => q.network === ARC && q.extra?.name !== BATCHED);
    if (!r) throw new Error("no plain EIP-3009 option on Arc");
    return r;
  };
  const payer = { slot: emptyResult(), max: 0n, address: account.address };
  const client = x402Client.fromConfig({
    schemes: [],
    paymentRequirementsSelector: pickArc,
    spendControls: { maxAmountPerPayment: `$${maxUsd}`, allowedAssets: [{ network: ARC, asset: USDC }] },
  });
  registerExactEvmScheme(client, { signer: account, networks: [ARC], paymentRequirementsSelector: pickArc });
  client.onBeforePaymentCreation(capHook(payer));
  payer.fetch = wrapFetchWithPayment(fetch, client);
  return payer;
}

export async function payExact(payer, t) {
  const out = (payer.slot = emptyResult());
  payer.max = atomic(t.max);
  const t0 = performance.now();
  try {
    const res = await payer.fetch(t.resource, { method: t.method, ...bodyInit(t) });
    const text = await res.text();
    out.status = res.status;
    out.bodyBytes = Buffer.byteLength(text);
    out.bodySha256 = sha256(text);
    out.delivered = deliveredFrom(res.status, text);
    if (!out.delivered) out.errorBody = text.slice(0, 240);
    const h = res.headers.get("payment-response");
    if (h) { try { out.settle = decodePaymentResponseHeader(h); } catch { out.settle = { raw: h }; } }
    if (res.status === 402) out.error = "still 402 after payment attempt";
  } catch (e) {
    out.error = e.message;
  }
  out.ms = Math.round(performance.now() - t0);
  return out;
}
