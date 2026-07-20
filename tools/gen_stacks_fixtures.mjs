// tools/gen_stacks_fixtures.mjs — fixture generator for agentpay/_stacks_tx.py
// (AGE-22). Oracle: @stacks/transactions v7 — serialized txs must match
// byte-for-byte (RFC6979 deterministic signatures on both sides).
//
// Usage:
//   cd tools && npm install @stacks/transactions c32check
//   node gen_stacks_fixtures.mjs      # writes tests/fixtures/stacks_tx_fixtures.json
//
// Test keys are throwaway constants — NEVER fund them.
import {
  makeContractCall,
  Cl,
  Pc,
  PostConditionMode,
  getAddressFromPrivateKey,
  privateKeyToPublic,
  sigHashPreSign,
  TransactionSigner,
  makeUnsignedContractCall,
} from '@stacks/transactions';
import { c32address, c32addressDecode } from 'c32check';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const OUT = path.join(path.dirname(fileURLToPath(import.meta.url)),
  '..', 'tests', 'fixtures', 'stacks_tx_fixtures.json');

const SBTC_TESTNET = 'ST1F7QA2MDF17S807EPA36TSS8AMEFY4KA9TVGWXT.sbtc-token';
const SBTC_MAINNET = 'SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token';

// Deterministic test keys (NEVER real funds). Trailing 01 = compressed.
const KEYS = [
  '0000000000000000000000000000000000000000000000000000000000000001' + '01',
  'b244296d5907de9864c0b0d51f98a13c52890be0404e83f273144cd5b9960eed' + '01',
  'edf9aee84d9b7abc145504dde6726c64f369d37ee34ded868fabd876c26570bc' + '01',
  // raw 64-hex (no '01' suffix) => UNCOMPRESSED pubkey + key-encoding 0x01
  'b244296d5907de9864c0b0d51f98a13c52890be0404e83f273144cd5b9960eed',
];

const fixtures = { keys: [], c32_vectors: [], transactions: [] };

for (const k of KEYS) {
  const pub = privateKeyToPublic(k);
  fixtures.keys.push({
    private_key: k,
    public_key: pub,
    address_mainnet: getAddressFromPrivateKey(k, 'mainnet'),
    address_testnet: getAddressFromPrivateKey(k, 'testnet'),
  });
}

// c32 vectors incl. leading-zero hash160s
const h160s = [
  '0000000000000000000000000000000000000000',
  '0000000000000000000000000000000000000001',
  '00000000000000000000000000000000000000ff',
  '1234567890abcdef1234567890abcdef12345678',
  'ffffffffffffffffffffffffffffffffffffffff',
  '00d68e8ba26bcd41c96e6b1a9ea4c9d1e15075ad',
];
for (const h of h160s) {
  for (const ver of [22, 26, 20, 21]) { // P2PKH main/test, P2SH main/test
    const addr = c32address(ver, h);
    const [dver, dh] = c32addressDecode(addr);
    if (dver !== ver || dh !== h) throw new Error('c32 roundtrip failed');
    fixtures.c32_vectors.push({ version: ver, hash160: h, address: addr });
  }
}

const senderKey = KEYS[0];
const senderAddrT = getAddressFromPrivateKey(senderKey, 'testnet');
const senderAddrM = getAddressFromPrivateKey(senderKey, 'mainnet');
const recipientT = getAddressFromPrivateKey(KEYS[1], 'testnet');
const recipientM = getAddressFromPrivateKey(KEYS[1], 'mainnet');

async function addTx(name, opts, meta) {
  const tx = await makeContractCall(opts);
  fixtures.transactions.push({
    name,
    ...meta,
    serialized_hex: tx.serialize(),
    txid: tx.txid(),
  });
  return tx;
}

function sbtcArgs(amount, sender, recipient, paymentId) {
  const memo = paymentId === null
    ? Cl.none()
    : Cl.some(Cl.bufferFromUtf8(paymentId));
  return [Cl.uint(amount), Cl.principal(sender), Cl.principal(recipient), memo];
}

const [tContract, tName] = SBTC_TESTNET.split('.');
const [mContract, mName] = SBTC_MAINNET.split('.');

// 1. canonical testnet transfer, memo = payment_id, deny-mode FT post-condition
await addTx('testnet_transfer_memo_pc', {
  contractAddress: tContract,
  contractName: tName,
  functionName: 'transfer',
  functionArgs: sbtcArgs(1500n, senderAddrT, recipientT, 'pay_7f3d2a1b9c8e4f5a6b7c8d9e0f1a'),
  senderKey,
  network: 'testnet',
  fee: 300n,
  nonce: 7n,
  postConditionMode: PostConditionMode.Deny,
  postConditions: [Pc.principal(senderAddrT).willSendEq(1500n).ft(SBTC_TESTNET, 'sbtc-token')],
}, {
  network: 'testnet', amount_sats: '1500', fee: '300', nonce: '7',
  sender: senderAddrT, recipient: recipientT,
  payment_id: 'pay_7f3d2a1b9c8e4f5a6b7c8d9e0f1a', sponsored: false,
  contract: SBTC_TESTNET,
});

// 2. mainnet variant
await addTx('mainnet_transfer_memo_pc', {
  contractAddress: mContract,
  contractName: mName,
  functionName: 'transfer',
  functionArgs: sbtcArgs(250000n, senderAddrM, recipientM, 'pay_min'),
  senderKey,
  network: 'mainnet',
  fee: 180n,
  nonce: 0n,
  postConditionMode: PostConditionMode.Deny,
  postConditions: [Pc.principal(senderAddrM).willSendEq(250000n).ft(SBTC_MAINNET, 'sbtc-token')],
}, {
  network: 'mainnet', amount_sats: '250000', fee: '180', nonce: '0',
  sender: senderAddrM, recipient: recipientM,
  payment_id: 'pay_min', sponsored: false,
  contract: SBTC_MAINNET,
});

// 3. sponsored variant (origin-signed only; sponsor unsigned placeholder)
await addTx('testnet_transfer_sponsored_origin_signed', {
  contractAddress: tContract,
  contractName: tName,
  functionName: 'transfer',
  functionArgs: sbtcArgs(42n, senderAddrT, recipientT, 'pay_sponsored_001'),
  senderKey,
  network: 'testnet',
  fee: 0n,
  nonce: 12n,
  sponsored: true,
  postConditionMode: PostConditionMode.Deny,
  postConditions: [Pc.principal(senderAddrT).willSendEq(42n).ft(SBTC_TESTNET, 'sbtc-token')],
}, {
  network: 'testnet', amount_sats: '42', fee: '0', nonce: '12',
  sender: senderAddrT, recipient: recipientT,
  payment_id: 'pay_sponsored_001', sponsored: true,
  contract: SBTC_TESTNET,
});

// 4. max-length memo (34 bytes) + large amount (>32-bit)
await addTx('testnet_transfer_memo34_large', {
  contractAddress: tContract,
  contractName: tName,
  functionName: 'transfer',
  functionArgs: sbtcArgs(21000000_00000000n, getAddressFromPrivateKey(KEYS[2], 'testnet'), recipientT, 'x'.repeat(34)),
  senderKey: KEYS[2],
  network: 'testnet',
  fee: 5000n,
  nonce: 4294967297n,
  postConditionMode: PostConditionMode.Deny,
  postConditions: [Pc.principal(getAddressFromPrivateKey(KEYS[2], 'testnet')).willSendEq(21000000_00000000n).ft(SBTC_TESTNET, 'sbtc-token')],
}, {
  network: 'testnet', amount_sats: '2100000000000000', fee: '5000', nonce: '4294967297',
  sender: getAddressFromPrivateKey(KEYS[2], 'testnet'), recipient: recipientT,
  payment_id: 'x'.repeat(34), sponsored: false,
  contract: SBTC_TESTNET,
});

// 5. unsigned serialization + presign sighash chain (standard auth)
{
  const unsigned = await makeUnsignedContractCall({
    contractAddress: tContract,
    contractName: tName,
    functionName: 'transfer',
    functionArgs: sbtcArgs(999n, senderAddrT, recipientT, 'pay_presign_check'),
    publicKey: privateKeyToPublic(senderKey),
    network: 'testnet',
    fee: 250n,
    nonce: 9n,
    postConditionMode: PostConditionMode.Deny,
    postConditions: [Pc.principal(senderAddrT).willSendEq(999n).ft(SBTC_TESTNET, 'sbtc-token')],
  });
  const unsignedHex = unsigned.serialize();
  const signBegin = unsigned.signBegin();
  const presign = sigHashPreSign(signBegin, unsigned.auth.authType, 250n, 9n);
  const signer = new TransactionSigner(unsigned);
  signer.signOrigin(senderKey);
  fixtures.presign = {
    unsigned_serialized_hex: unsignedHex,
    sign_begin: signBegin,
    presign_sighash: presign,
    fee: '250', nonce: '9',
    payment_id: 'pay_presign_check',
    amount_sats: '999',
    sender: senderAddrT, recipient: recipientT,
    contract: SBTC_TESTNET,
    private_key: senderKey,
    signed_serialized_hex: unsigned.serialize(),
    txid: unsigned.txid(),
  };
}

fs.writeFileSync(OUT, JSON.stringify(fixtures, null, 2));
console.log('fixtures written:',
  fixtures.transactions.length, 'txs,',
  fixtures.c32_vectors.length, 'c32 vectors,',
  fixtures.keys.length, 'keys');
