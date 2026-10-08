import { readFile } from "node:fs/promises";
import path from "node:path";
import solc from "solc";
import { ROOT, writeJson } from "../src/lib.mjs";

const source = await readFile(path.join(ROOT, "contracts", "ReceiptAnchor.sol"), "utf8");
const input = {
  language: "Solidity",
  sources: { "ReceiptAnchor.sol": { content: source } },
  settings: { optimizer: { enabled: true, runs: 200 }, evmVersion: "cancun", outputSelection: { "*": { "*": ["abi", "evm.bytecode.object", "metadata"] } } },
};
const out = JSON.parse(solc.compile(JSON.stringify(input)));
const errors = (out.errors ?? []).filter((e) => e.severity === "error");
if (errors.length) { console.error(errors.map((e) => e.formattedMessage).join("\n")); process.exit(1); }
const c = out.contracts["ReceiptAnchor.sol"].ReceiptAnchor;
await writeJson(path.join(ROOT, "build", "ReceiptAnchor.json"), { compiler: `solc ${solc.version()}`, abi: c.abi, bytecode: "0x" + c.evm.bytecode.object, metadata: c.metadata });
console.log(`compiled with solc ${solc.version()}, ${c.evm.bytecode.object.length / 2} bytes`);
