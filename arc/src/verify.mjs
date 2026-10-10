// Independent settlement checks for rows that came back without a PAYMENT-RESPONSE:
// Arc transfer logs for EIP-3009, Circle's Gateway transfer ledger for batched.
import { createPublicClient, http, parseAbiItem } from "viem";
import { arc } from "viem/chains";
import { USDC } from "./lib.mjs";

const TRANSFER = parseAbiItem("event Transfer(address indexed from, address indexed to, uint256 value)");

export function arcClient() {
  return createPublicClient({ chain: arc, transport: http() });
}

export async function onchainTransfers(pub, payer, fromBlock) {
  const toBlock = await pub.getBlockNumber();
  const logs = await pub.getLogs({ address: USDC, event: TRANSFER, args: { from: payer }, fromBlock, toBlock });
  return logs.map((l) => ({ to: l.args.to.toLowerCase(), amount: l.args.value.toString(), txHash: l.transactionHash, block: Number(l.blockNumber) }));
}

export async function gatewayTransfers(gateway, since) {
  const r = await gateway.client.searchTransfers({ from: gateway.address, pageSize: 50 });
  return (r.transfers ?? [])
    .filter((t) => new Date(t.createdAt) >= since)
    .map((t) => ({ to: String(t.toAddress).toLowerCase(), amount: String(t.amount), id: t.id, status: t.status, createdAt: t.createdAt }));
}

// Fills `settled` on receipt-less rows when a payer→payTo transfer of the charged amount exists in the
// row's own window (block after the call for EIP-3009, ±2 min around the call for Gateway).
export function reconcile(rows, chain, gw) {
  const used = new Set();
  for (const r of rows) {
    const p = r.paid;
    if (!p || p.settle?.transaction || !r.listed?.payTo || !p.charged) continue;
    const payTo = r.listed.payTo.toLowerCase();
    const t0 = p.paidAt ? new Date(p.paidAt).getTime() : null;
    const inWindow = (t) => r.domain === "batched"
      ? (t0 === null || Math.abs(new Date(t.createdAt).getTime() - t0) <= 120_000)
      : (p.blockBefore == null || (t.block >= p.blockBefore && t.block <= p.blockBefore + 600));
    const pool = r.domain === "batched" ? gw : chain;
    const hit = pool.find((t) => !used.has(t.id ?? t.txHash) && t.to === payTo && t.amount === p.charged && inWindow(t));
    if (!hit) { p.settled = null; continue; }
    used.add(hit.id ?? hit.txHash);
    p.settled = r.domain === "batched" ? { via: "gateway", id: hit.id, status: hit.status, createdAt: hit.createdAt } : { via: "onchain", txHash: hit.txHash, block: hit.block };
    r.proof = r.domain === "batched" ? `gateway:transfer:${hit.id}` : `eip3009:tx:${hit.txHash}`;
  }
}
