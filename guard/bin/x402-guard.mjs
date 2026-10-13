#!/usr/bin/env node
// x402-guard check <url> [...]  — prints one decision per URL; exit 2 if any blocks.
import { checkUrl, createScoreSource, SCORES_URL } from '../src/index.mjs';

const args = process.argv.slice(2);
const cmd = args.shift();
if (cmd !== 'check' || args.length === 0) {
  console.error('usage: x402-guard check <resource-url> [more-urls]   (env X402_GUARD_SCORES_URL overrides the source)');
  process.exit(1);
}

const source = createScoreSource({ url: process.env.X402_GUARD_SCORES_URL || SCORES_URL });
let blocked = false;
for (const url of args) {
  const d = await checkUrl(url, { source });
  blocked ||= d.verdict === 'block';
  const { entry, ...rest } = d;
  console.log(JSON.stringify({ ...rest, probes: entry?.paid_probes ?? 0, last_ok_at: entry?.last_ok_at ?? null }));
}
if (source.lastError) console.error(`warning: scores.json unavailable (${source.lastError.message}); verdicts are "unknown"`);
process.exit(blocked ? 2 : 0);
