import { clients, loadArtifact } from "../src/anchor.mjs";
import { addressUrl, txUrl } from "../src/lib.mjs";

const key = process.env.ARC_PRIVATE_KEY;
if (!key) { console.error("ARC_PRIVATE_KEY missing"); process.exit(1); }
const { abi, bytecode } = await loadArtifact();
const { account, pub, wallet } = clients(key);
console.log(`deploying ReceiptAnchor from ${account.address}`);
const hash = await wallet.deployContract({ abi, bytecode });
const rcpt = await pub.waitForTransactionReceipt({ hash });
console.log(`tx       ${txUrl(hash)}`);
console.log(`contract ${rcpt.contractAddress}  ${addressUrl(rcpt.contractAddress)}`);
console.log(`\nadd to .env:\nARC_ANCHOR_ADDRESS=${rcpt.contractAddress}`);
