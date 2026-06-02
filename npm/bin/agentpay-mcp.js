#!/usr/bin/env node

const { execSync, spawn } = require('child_process');
const https = require('https');
const path = require('path');
const fs = require('fs');

const GATEWAY_URL = process.env.AGENTPAY_GATEWAY_URL ||
  'https://agentpay.tools';

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

  // Resolve the gateway's actual network ONCE up front. Every wallet path
  // below needs it: a wallet keyed to one network but pointed at a gateway
  // on the other produces a confusing raw Horizon 404 on the first tool call
  // (the gateway looks the wallet's account up on the wrong network's
  // Horizon). We fail fast here with a clear message instead.
  let gatewayNetwork = null;
  try {
    const health = await checkGatewayNetwork();
    gatewayNetwork = health.network;  // "mainnet" | "testnet"
  } catch (err) {
    console.error('⚠️  Could not reach gateway /health:', err.message);
    console.error('   Proceeding without a network-match check.');
  }

  // Wallet selection. AgentPay leads with 17 free tools that need no funding
  // and no on-chain settlement, so the default path is ZERO-SETUP against the
  // (mainnet) gateway — no faucet, no testnet, no wallet. A funded wallet is
  // only needed for the one paid tool (session_create).
  //
  // 1. STELLAR_SECRET_KEY set → use Stellar, validate network vs gateway.
  // 2. BASE_PRIVATE_KEY set → use Base mainnet.
  // 3. Nothing set → DEFAULT: no wallet; the MCP server mints an ephemeral
  //    keypair so free tools just work. Opt into the testnet faucet explicitly
  //    with AGENTPAY_USE_FAUCET=1 (only useful for testing the paid tool).

  const wantFaucet = ['1', 'true', 'yes', 'on'].includes(
    String(process.env.AGENTPAY_USE_FAUCET || '').toLowerCase()
  );

  if (process.env.STELLAR_SECRET_KEY) {
    // If STELLAR_NETWORK is set explicitly and disagrees with the gateway,
    // refuse to start — this is the misconfiguration that surfaces as a raw
    // Horizon 404 mid-session. If it's unset, default it to the gateway's
    // network so the wallet is interpreted on the right Horizon.
    const explicitNet = process.env.STELLAR_NETWORK;
    if (gatewayNetwork && explicitNet && explicitNet !== gatewayNetwork) {
      console.error('');
      console.error('❌ Network mismatch.');
      console.error(`   STELLAR_NETWORK=${explicitNet} but the gateway at`);
      console.error(`   ${GATEWAY_URL} is running on ${gatewayNetwork}.`);
      console.error('');
      console.error(`   A ${explicitNet} wallet cannot pay a ${gatewayNetwork} gateway —`);
      console.error('   the gateway would look your account up on the wrong');
      console.error('   network and every tool call would fail with a 404.');
      console.error('');
      console.error(`   Fix: use a ${gatewayNetwork} STELLAR_SECRET_KEY, or point at a`);
      console.error(`   ${explicitNet} gateway via AGENTPAY_GATEWAY_URL.`);
      console.error('');
      process.exit(1);
    }
    const network = explicitNet || gatewayNetwork || 'testnet';
    env.STELLAR_NETWORK = network;
    console.log(`✓ Using Stellar wallet (${network})`);

  } else if (process.env.BASE_PRIVATE_KEY) {
    console.log('✓ Using Base mainnet wallet');

  } else if (wantFaucet) {
    // Explicit opt-in: mint a funded testnet wallet via the faucet. Only
    // useful for testing the paid session_create tool without real USDC, and
    // only works against the testnet gateway (the faucet 404s on mainnet).
    if (gatewayNetwork === 'mainnet') {
      console.error('');
      console.error('❌ AGENTPAY_USE_FAUCET is set but the gateway is on mainnet.');
      console.error('   The faucet is testnet-only. Either:');
      console.error('     • point at the testnet gateway:');
      console.error('       AGENTPAY_GATEWAY_URL=https://gateway-testnet-production.up.railway.app');
      console.error('     • or set a funded mainnet STELLAR_SECRET_KEY instead.');
      console.error('');
      process.exit(1);
    }
    console.log('🪙 AGENTPAY_USE_FAUCET set — creating a funded testnet wallet...');
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
      env.STELLAR_SECRET_KEY = wallet.secret_key;
      env.STELLAR_NETWORK = 'testnet';
    } catch (err) {
      console.error('❌ Failed to create testnet wallet:', err.message);
      console.error('   Set STELLAR_SECRET_KEY or BASE_PRIVATE_KEY manually.');
      process.exit(1);
    }

  } else {
    // DEFAULT zero-setup path: no wallet. The MCP server mints an ephemeral
    // keypair so all 17 free tools work immediately against the gateway with
    // no funding, no faucet, no network choice. A funded mainnet wallet is
    // only needed for the paid session_create tool.
    console.log('🆓 No wallet configured — running in free-tools mode (zero setup).');
    console.log('   All 17 free tools work as-is. For the paid session_create tool,');
    console.log('   set STELLAR_SECRET_KEY to a funded mainnet wallet, or set');
    console.log('   AGENTPAY_USE_FAUCET=1 against the testnet gateway to test it.');
  }

  env.AGENTPAY_GATEWAY_URL = GATEWAY_URL;

  console.log('🚀 Starting AgentPay MCP server...');
  console.log(`   Gateway: ${GATEWAY_URL}`);
  console.log(`   Tools: 18 tools (17 free)`);
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
