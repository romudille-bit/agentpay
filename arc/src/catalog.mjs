// Pulls every Arc listing from Circle's discovery API and writes a shape-only summary (no raw dump).
import path from "node:path";
import { ARC, DISCOVERY, DOCS, ROOT, arcOptions, domainOf, now, readJson, usd, writeJson } from "./lib.mjs";

export async function pullCatalog() {
  const items = [];
  for (let offset = 0; ; offset += 200) {
    const page = (await getPage(offset)).items ?? [];
    items.push(...page);
    if (page.length < 200) break;
    await new Promise((r) => setTimeout(r, 400));
  }
  return items;
}

// The discovery API rate-limits bursts; back off on 429.
async function getPage(offset, attempt = 0) {
  const res = await fetch(`${DISCOVERY}?network=${ARC}&limit=200&offset=${offset}`);
  if (res.status === 429 && attempt < 6) {
    const wait = Number(res.headers.get("retry-after") ?? 0) * 1000 || 2000 * 2 ** attempt;
    await new Promise((r) => setTimeout(r, wait));
    return getPage(offset, attempt + 1);
  }
  if (!res.ok) throw new Error(`discovery ${res.status}`);
  return res.json();
}

export function summarize(items) {
  const providers = new Map();
  let batched = 0, eip3009 = 0, arcOnly = 0;
  const prices = [];
  for (const it of items) {
    const opts = arcOptions(it.accepts);
    if (it.accepts.every((a) => a.network === ARC)) arcOnly++;
    const name = it.metadata?.provider?.name ?? new URL(it.resource).host;
    const p = providers.get(name) ?? { name, hosts: new Set(), listings: 0, batched: 0, eip3009: 0, prices: [] };
    p.listings++;
    p.hosts.add(new URL(it.resource).host);
    for (const o of opts) {
      domainOf(o) === "batched" ? (p.batched++, batched++) : (p.eip3009++, eip3009++);
      p.prices.push(Number(o.amount));
    }
    prices.push(Math.min(...opts.map((o) => Number(o.amount))));
    providers.set(name, p);
  }
  const median = (xs) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : 0; };
  return {
    pulledAt: now(),
    network: ARC,
    listings: items.length,
    arcOnly,
    domains: { batched, eip3009 },
    priceUsd: { median: usd(median(prices)), atOrUnder1c: prices.filter((p) => p <= 10_000).length },
    providers: [...providers.values()]
      .map((p) => ({ name: p.name, hosts: [...p.hosts].sort(), listings: p.listings, batched: p.batched, eip3009: p.eip3009, minUsd: usd(Math.min(...p.prices)), medianUsd: usd(median(p.prices)) }))
      .sort((a, b) => b.listings - a.listings),
  };
}

// Listed price + input schema for each probe target, taken from the catalog so the ledger can show listed vs charged.
export function matchTargets(items, targets) {
  const byUrl = new Map(items.map((it) => [it.resource, it]));
  return targets.map((t) => {
    const base = t.resource.split("?")[0];
    const it = byUrl.get(base) ?? items.find((i) => base.startsWith(i.resource.replace(/\{[^}]+\}.*$/, "")));
    if (!it) return { ...t, listed: null };
    const opt = arcOptions(it.accepts).find((o) => domainOf(o) === t.domain);
    return { ...t, listed: opt ? { amount: opt.amount, usd: usd(opt.amount), payTo: opt.payTo, siwx: it.metadata?.siwx ?? false, lastUpdated: it.lastUpdated } : null };
  });
}

if (process.argv[1] && import.meta.url.endsWith(path.basename(process.argv[1]))) {
  const items = await pullCatalog();
  const summary = summarize(items);
  await writeJson(path.join(DOCS, "catalog.json"), summary);
  const targets = matchTargets(items, await readJson(path.join(ROOT, "targets.json"), []));
  await writeJson(path.join(ROOT, "build", "targets.resolved.json"), targets);
  console.log(`${summary.listings} listings, ${summary.providers.length} providers, batched ${summary.domains.batched} / eip3009 ${summary.domains.eip3009}`);
  for (const t of targets) console.log(`${t.listed ? "ok " : "?? "} ${t.id} listed=${t.listed?.usd ?? "not in catalog"}`);
}
