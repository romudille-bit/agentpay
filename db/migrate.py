#!/usr/bin/env python3
"""
db/migrate.py — Create Supabase tables and seed all 9 AgentPay tools.

Run from project root:
    source venv/bin/activate
    python db/migrate.py
"""
import sys
import os
import asyncio
import json
import httpx

# ── Resolve project root so registry imports work ─────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "registry"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://twdtvssqfpgydsvwqglt.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_zpw1bVbwENxB1LAlq-v3nA_cVk6Uv8U")

# ── DDL ───────────────────────────────────────────────────────────────────────

TOOLS_DDL = """
CREATE TABLE IF NOT EXISTS tools (
    id              SERIAL PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    description     TEXT        DEFAULT '',
    endpoint        TEXT        DEFAULT '',
    price_usdc      TEXT        DEFAULT '0',
    developer_address TEXT      DEFAULT '',
    parameters      JSONB       DEFAULT '{}'::jsonb,
    category        TEXT        DEFAULT 'data',
    active          BOOLEAN     DEFAULT true,
    uptime_pct      FLOAT       DEFAULT 100.0,
    total_calls     INTEGER     DEFAULT 0,
    triggers        JSONB       DEFAULT '[]'::jsonb,
    use_when        TEXT        DEFAULT '',
    returns         TEXT        DEFAULT ''
);
"""

LOGS_DDL = """
CREATE TABLE IF NOT EXISTS payment_logs (
    id            SERIAL PRIMARY KEY,
    payment_id    TEXT        UNIQUE,
    tool_name     TEXT        NOT NULL,
    agent_address TEXT        NOT NULL,
    amount_usdc   TEXT        NOT NULL,
    tx_hash       TEXT,
    timestamp     TIMESTAMPTZ DEFAULT NOW(),
    status        TEXT        DEFAULT 'completed'
);
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


async def _run_sql(client: httpx.AsyncClient, sql: str, label: str) -> bool:
    """Try to execute DDL via Supabase REST SQL endpoint (service_role key required)."""
    resp = await client.post(
        f"{SUPABASE_URL}/rest/v1/sql",
        headers=_headers(),
        json={"query": sql},
        timeout=15.0,
    )
    if resp.status_code in (200, 201):
        print(f"  ✓ {label}")
        return True
    print(f"  ✗ {label}: {resp.status_code} — {resp.text[:200]}")
    return False


async def _upsert_tools(client: httpx.AsyncClient, tools_data: list[dict]) -> bool:
    resp = await client.post(
        f"{SUPABASE_URL}/rest/v1/tools",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=tools_data,
        timeout=15.0,
    )
    if resp.status_code in (200, 201):
        print(f"  ✓ Upserted {len(tools_data)} tools")
        return True
    print(f"  ✗ Upsert failed: {resp.status_code} — {resp.text[:300]}")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    from registry import list_tools

    print(f"\nAgentPay — Supabase migration")
    print(f"URL: {SUPABASE_URL}\n")

    async with httpx.AsyncClient() as client:

        # ── 1. Create tables ──────────────────────────────────────────────────
        print("── Creating tables ──")
        tools_ok = await _run_sql(client, TOOLS_DDL, "CREATE TABLE tools")
        logs_ok  = await _run_sql(client, LOGS_DDL,  "CREATE TABLE payment_logs")

        if not (tools_ok and logs_ok):
            print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The REST SQL endpoint requires a service_role key.
Run the following SQL in your Supabase SQL Editor:
  https://supabase.com/dashboard/project/twdtvssqfpgydsvwqglt/sql

── tools ──────────────────────────────────────────────────""")
            print(TOOLS_DDL)
            print("── payment_logs ───────────────────────────────────────────")
            print(LOGS_DDL)
            print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            print("\nAfter running the SQL, re-run this script to seed the tools.")
            print("(Attempting seed anyway in case tables already exist…)\n")

        # ── 2. Seed tools ─────────────────────────────────────────────────────
        print("── Seeding tools ──")
        tools = list_tools()
        tools_data = [
            {
                "name":              t.name,
                "description":       t.description,
                "endpoint":          t.endpoint,
                "price_usdc":        t.price_usdc,
                "developer_address": t.developer_address,
                "parameters":        t.parameters,
                "category":          t.category,
                "active":            t.active,
                "uptime_pct":        t.uptime_pct,
                "total_calls":       t.total_calls,
                "triggers":          t.triggers,
                "use_when":          t.use_when,
                "returns":           t.returns,
            }
            for t in tools
        ]
        await _upsert_tools(client, tools_data)

        # ── 3. Verify ─────────────────────────────────────────────────────────
        print("\n── Verifying tools in database ──")
        verify = await client.get(
            f"{SUPABASE_URL}/rest/v1/tools",
            headers={**_headers(), "Prefer": "count=exact"},
            params={"select": "name,price_usdc,category", "order": "name"},
            timeout=10.0,
        )
        if verify.status_code == 200:
            rows = verify.json()
            print(f"  ✓ {len(rows)} tools found in database:")
            for r in rows:
                print(f"      {r['name']:<22} ${r['price_usdc']}  [{r['category']}]")
        else:
            print(f"  ✗ Verify failed: {verify.status_code} — {verify.text[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
