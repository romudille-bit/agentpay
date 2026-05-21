#!/usr/bin/env node

const { execSync, spawn } = require('child_process');
const https = require('https');
const path = require('path');
const fs = require('fs');

const GATEWAY_URL = process.env.AGENTPAY_GATEWAY_URL ||
  'https://gateway-production-2cc2.up.railway.app';

// ── 1. Check Python is available ─────────────────────
function getPython() {
  for (const cmd of ['python3', 'python']) {
    try {
      const v = execSync(`${cmd} --version 2>&1`).toString();
      if (v.includes('Python 3')) return cmd;
    } catch {}
  }
  console.error('❌ Python 3 is required. Install from https://python.org');
  process.exit(1);
}

// ── 2. Ensure Python deps are installed ──────────────
function ensureDeps(python) {
  const deps = ['mcp', 'stellar-sdk', 'httpx', 'pydantic-settings'];
  for (const dep of deps) {
    try {
      execSync(`${python} -c "import ${dep.replace('-', '_').replace('-sdk','')}"`,
        { stdio: 'ignore' });
    } catch {
      console.log(`📦 Installing ${dep}...`);
      try {
        execSync(
          `${python} -m pip install ${dep} --break-system-packages -q`,
          { stdio: 'inherit' }
        );
      } catch {
        execSync(
          `${python} -m pip install ${dep} -q`,
          { stdio: 'inherit' }
        );
      }
    }
  }
}

// ── 3. Find mcp_server.py ─────────────────────────────
function findMcpServer() {
  const candidates = [
    path.join(__dirname, '../../gateway/mcp_server.py'),
    path.join(process.cwd(), 'gateway/mcp_server.py'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  console.error('❌ Could not find gateway/mcp_server.py');
  console.error('   Make sure you are running from the agentpay repo root.');
  process.exit(1);
}

// ── 4. Check gateway network ──────────────────────────
function checkGatewayNetwork() {
  return new Promise((resolve, reject) => {
    https.get(`${GATEWAY_URL}/health`, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch {
          reject(new Error('Failed to parse /health response'));
        }
      });
    }).on('error', reject);
  });
}

// ── 5. Call faucet to create a new testnet wallet ────
function createTestnetWallet() {
  return new Promise((resolve, reject) => {
    https.get(`${GATEWAY_URL}/faucet`, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch {
          reject(new Error('Failed to parse faucet response'));
        }
      });
    }).on('error', reject);
  });
}

// ── 5. Main ───────────────────────────────────────────
async function main() {
  const python = getPython();
  ensureDeps(python);
  const mcpServer = findMcpServer();

  const env = { ...process.env };

  // Payment method priority:
  // 1. STELLAR_SECRET_KEY set → use Stellar (testnet or mainnet via STELLAR_NETWORK)
  // 2. BASE_PRIVATE_KEY set → use Base mainnet
  // 3. Neither set → auto-create testnet Stellar wallet via faucet

  if (process.env.STELLAR_SECRET_KEY) {
    const network = process.env.STELLAR_NETWORK || 'testnet';
    console.log(`✓ Using Stellar wallet (${network})`);

  } else if (process.env.BASE_PRIVATE_KEY) {
    console.log('✓ Using Base mainnet wallet');

  } else {
    console.log('🔍 Checking gateway network...');
    try {
      const health = await checkGatewayNetwork();
      if (health.network === 'mainnet') {
        console.log('');
        console.log('⚠️  This gateway is running on mainnet.');
        console.log('   The faucet is not available on mainnet.');
        console.log('');
        console.log('   To use AgentPay on mainnet, fund a Stellar wallet');
        console.log('   with USDC and set your secret key:');
        console.log('');
        console.log('     STELLAR_SECRET_KEY=<your-secret-key> npx agentpay-mcp');
        console.log('');
        console.log('   Docs: https://github.com/romudille-bit/agentpay');
        process.exit(0);
      }
    } catch (err) {
      console.error('⚠️  Could not reach gateway /health:', err.message);
    }
    console.log('🪙 No wallet configured — creating a free testnet wallet...');
    try {
      const wallet = await createTestnetWallet();
      console.log('');
      console.log('✅ Testnet wallet created and funded!');
      console.log(`   Public key:  ${wallet.public_key}`);
      console.log(`   USDC balance: ${wallet.usdc_balance} (testnet)`);
      console.log('');
      console.log('💾 Save your secret key to reuse this wallet:');
      console.log(`   STELLAR_SECRET_KEY=${wallet.secret_key}`);
      console.log('');
      console.log('⚠️  This is a TESTNET wallet. For mainnet, fund a real');
      console.log('   Stellar wallet and set STELLAR_SECRET_KEY.');
      console.log('');
      env.STELLAR_SECRET_KEY = wallet.secret_key;
      env.STELLAR_NETWORK = 'testnet';
    } catch (err) {
      console.error('❌ Failed to create testnet wallet:', err.message);
      console.error('   Set STELLAR_SECRET_KEY or BASE_PRIVATE_KEY manually.');
      process.exit(1);
    }
  }

  env.AGENTPAY_GATEWAY_URL = GATEWAY_URL;

  console.log('🚀 Starting AgentPay MCP server...');
  console.log(`   Gateway: ${GATEWAY_URL}`);
  console.log(`   Tools: 12 crypto data tools`);
  console.log('');

  const proc = spawn(python, [mcpServer], {
    env,
    stdio: 'inherit',
  });

  proc.on('exit', code => process.exit(code ?? 0));
  process.on('SIGINT', () => proc.kill('SIGINT'));
  process.on('SIGTERM', () => proc.kill('SIGTERM'));
}

main().catch(err => {
  console.error('❌ Error:', err.message);
  process.exit(1);
});
