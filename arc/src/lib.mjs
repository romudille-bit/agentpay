import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const DOCS = path.join(ROOT, "docs");
export const ARC = "eip155:5042";
export const USDC = "0x3600000000000000000000000000000000000000";
export const DISCOVERY = "https://api.circle.com/v2/x402/discovery/resources";
export const EXPLORER = "https://explorer.arc.io";
export const BATCHED = "GatewayWalletBatched";

export const usd = (atomic) => (Number(atomic) / 1e6).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
export const sha256 = (s) => createHash("sha256").update(s).digest("hex");
export const now = () => new Date().toISOString();

export async function readJson(p, fallback) {
  try { return JSON.parse(await readFile(p, "utf8")); } catch { return fallback; }
}
export async function writeJson(p, data) {
  await mkdir(path.dirname(p), { recursive: true });
  await writeFile(p, JSON.stringify(data, null, 2) + "\n");
}

export function arcOptions(accepts = []) {
  return accepts.filter((a) => a.network === ARC);
}
// "batched" = Circle Gateway (off-chain authorization, netted settlement); "eip3009" = plain USDC transferWithAuthorization.
export function domainOf(option) {
  return option?.extra?.name === BATCHED ? "batched" : "eip3009";
}

export function decode402(header) {
  if (!header) return null;
  try { return JSON.parse(Buffer.from(header, "base64").toString("utf8")); } catch { return null; }
}

export async function timedFetch(url, init = {}, timeoutMs = 30_000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const t0 = performance.now();
  try {
    const res = await fetch(url, { ...init, signal: ctrl.signal });
    const text = await res.text();
    return { res, text, ms: Math.round(performance.now() - t0) };
  } finally { clearTimeout(timer); }
}

export function txUrl(hash) { return `${EXPLORER}/tx/${hash}`; }
export function addressUrl(a) { return `${EXPLORER}/address/${a}`; }
