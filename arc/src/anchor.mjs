// Writes one Receipt event per paid probe to the ReceiptAnchor contract on Arc.
import { readFile } from "node:fs/promises";
import path from "node:path";
import { createPublicClient, createWalletClient, http, keccak256, stringToHex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { arc } from "viem/chains";
import { ROOT } from "./lib.mjs";

export async function loadArtifact() {
  return JSON.parse(await readFile(path.join(ROOT, "build", "ReceiptAnchor.json"), "utf8"));
}

export function clients(privateKey) {
  const account = privateKeyToAccount(privateKey);
  const transport = http();
  return {
    account,
    pub: createPublicClient({ chain: arc, transport }),
    wallet: createWalletClient({ account, chain: arc, transport }),
  };
}

export const endpointHash = (url) => keccak256(stringToHex(url));
export const receiptHash = (receipt) => keccak256(stringToHex(typeof receipt === "string" ? receipt : JSON.stringify(receipt ?? "")));

export async function attest({ wallet, pub }, address, row) {
  const { abi } = await loadArtifact();
  const hash = await wallet.writeContract({
    address,
    abi,
    functionName: "attest",
    args: [endpointHash(row.resource), receiptHash(row.paid.settle), BigInt(row.paid.charged ?? 0), row.paid.delivered, row.proof],
  });
  const rcpt = await pub.waitForTransactionReceipt({ hash });
  return { txHash: hash, block: Number(rcpt.blockNumber), status: rcpt.status };
}
