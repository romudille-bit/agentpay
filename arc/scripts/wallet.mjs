// `npm run wallet` prints the probe address + balances. `npm run wallet -- new` mints a fresh key (prints once, never stores).
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { makeGateway } from "../src/pay.mjs";
import { addressUrl } from "../src/lib.mjs";

if (process.argv[2] === "new") {
  const pk = generatePrivateKey();
  console.log(`address  ${privateKeyToAccount(pk).address}\nARC_PRIVATE_KEY=${pk}`);
  process.exit(0);
}
const key = process.env.ARC_PRIVATE_KEY;
if (!key) { console.error("ARC_PRIVATE_KEY missing"); process.exit(1); }
const g = makeGateway(key);
const b = await g.client.getBalances();
console.log(`address  ${g.address}  ${addressUrl(g.address)}`);
console.log(`wallet   ${b.wallet.formatted} USDC`);
console.log(`gateway  ${b.gateway.formattedAvailable} USDC available (${b.gateway.formattedTotal} total)`);
