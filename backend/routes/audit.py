"""Audit log endpoint."""
from fastapi import APIRouter, HTTPException

from db import audit_log, llm_traces

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
async def list_audit(project_id: str | None = None, limit: int = 200):
    q = {}
    if project_id:
        q["project_id"] = project_id
    docs = await audit_log.find(q, {"_id": 0}).sort("at", -1).to_list(limit)
    return docs


# iter-13.101 — Detail Log Trace for an LLM call.
#
# The Audit page renders a "Detail Log Trace" button on every audit row.
# When clicked it calls this endpoint with the `trace_id` carried in the
# row's `details.trace_id`. The response shape matches the contract the
# user requested:
#   {
#     request_prompt, response_prompt,
#     request_time, response_time, elapsed_ms,
#     stage, agent_key,
#     status: "SUCCESS" | "FAIL",
#     error_reason,
#     ...
#   }
@router.get("/trace/{trace_id}")
async def get_trace(trace_id: str):
    doc = await llm_traces.find_one({"trace_id": trace_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail=f"trace not found: {trace_id}")
    return doc


# iter-13.101 — Lean trace headers for a project (debug tooling). The Audit
# page itself navigates via trace_id from the row's details, so this is
# mostly for the Console / curl debugging.
@router.get("/traces")
async def list_traces(project_id: str | None = None, limit: int = 100):
    q = {}
    if project_id:
        q["project_id"] = project_id
    cursor = llm_traces.find(
        q,
        {
            "_id": 0,
            "trace_id": 1,
            "project_id": 1,
            "stage": 1,
            "agent_key": 1,
            "status": 1,
            "error_reason": 1,
            "request_time": 1,
            "response_time": 1,
            "elapsed_ms": 1,
            "response_model": 1,
        },
    ).sort("request_time", -1)
    return await cursor.to_list(limit)
