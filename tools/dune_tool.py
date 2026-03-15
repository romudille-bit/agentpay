"""
dune_tool.py — Dune Analytics tool server for AgentPay.

Accepts a Dune query_id, executes or fetches latest results via the
Dune API, and returns the rows to the gateway.

Run with:
    uvicorn dune_tool:app --port 8002
"""

import os
import logging
import httpx
from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

DUNE_API_BASE = "https://api.dune.com/api/v1"

app = FastAPI(title="Dune Analytics Tool", version="0.1.0")


class ToolCallBody(BaseModel):
    parameters: dict = {}


@app.post("/")
async def run_dune_query(body: ToolCallBody):
    """
    Execute a Dune Analytics query and return live onchain results.

    Parameters:
        query_id (int | str): The Dune query ID to execute.
        query_parameters (dict, optional): Named parameters to pass to the query.
        limit (int, optional): Max rows to return (default 25).
    """
    api_key = os.environ.get("DUNE_API_KEY", "")
    if not api_key:
        return {"error": "DUNE_API_KEY not configured"}

    params = body.parameters
    query_id = params.get("query_id")
    if not query_id:
        return {"error": "query_id parameter is required"}

    query_parameters = params.get("query_parameters", {})
    limit = int(params.get("limit", 25))

    headers = {"X-DUNE-API-KEY": api_key}

    import asyncio

    async with httpx.AsyncClient(timeout=90.0) as client:
        # Step 1: Try to fetch latest cached results first (fast path)
        if not query_parameters:
            cached_resp = await client.get(
                f"{DUNE_API_BASE}/query/{query_id}/results",
                headers=headers,
                params={"limit": limit},
            )
            if cached_resp.status_code == 200:
                data = cached_resp.json()
                if data.get("is_execution_finished") and data.get("state") == "QUERY_STATE_COMPLETED":
                    result_data = data.get("result", {})
                    rows = result_data.get("rows", [])
                    metadata = result_data.get("metadata", {})
                    return {
                        "query_id": query_id,
                        "execution_id": data.get("execution_id", ""),
                        "row_count": len(rows),
                        "columns": metadata.get("column_names", []),
                        "rows": rows,
                        "generated_at": data.get("execution_ended_at", ""),
                        "source": "cached",
                    }

        # Step 2: Execute the query (with optional parameters or if no cache)
        execute_payload = {}
        if query_parameters:
            execute_payload["query_parameters"] = query_parameters

        exec_resp = await client.post(
            f"{DUNE_API_BASE}/query/{query_id}/execute",
            headers=headers,
            json=execute_payload,
        )
        if exec_resp.status_code != 200:
            return {
                "error": f"Dune execute failed: {exec_resp.status_code}",
                "detail": exec_resp.text,
            }

        execution_id = exec_resp.json().get("execution_id")
        if not execution_id:
            return {"error": "No execution_id returned from Dune"}

        # Step 3: Poll for results (retry up to ~90s)
        state = ""
        for attempt in range(45):
            results_resp = await client.get(
                f"{DUNE_API_BASE}/execution/{execution_id}/results",
                headers=headers,
                params={"limit": limit},
            )
            if results_resp.status_code != 200:
                return {
                    "error": f"Dune results fetch failed: {results_resp.status_code}",
                    "detail": results_resp.text,
                }

            data = results_resp.json()
            state = data.get("state", "")

            if state == "QUERY_STATE_COMPLETED":
                result_data = data.get("result", {})
                rows = result_data.get("rows", [])
                metadata = result_data.get("metadata", {})
                return {
                    "query_id": query_id,
                    "execution_id": execution_id,
                    "row_count": len(rows),
                    "columns": metadata.get("column_names", []),
                    "rows": rows,
                    "generated_at": data.get("submitted_at", ""),
                    "source": "fresh",
                }
            elif state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                return {
                    "error": f"Query ended with state: {state}",
                    "execution_id": execution_id,
                }

            await asyncio.sleep(2)

        return {
            "error": "Query timed out waiting for results",
            "execution_id": execution_id,
            "state": state,
        }


@app.get("/health")
async def health():
    has_key = bool(os.environ.get("DUNE_API_KEY"))
    return {"status": "ok", "dune_api_key_set": has_key}
