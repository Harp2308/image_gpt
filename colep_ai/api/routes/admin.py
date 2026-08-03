"""
api/routes/admin.py

Dashboard endpoints for query log inspection.

GET  /admin/stats                    — aggregated header stats
GET  /admin/logs                     — paginated log list with filters
GET  /admin/logs/{log_id}            — full log detail including context
POST /admin/logs/{log_id}/feedback   — set up/down feedback
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from colep_ai.database.query_log_client import (
    get_query_logs_container,
    list_logs,
    get_log_by_id,
    get_stats,
    set_feedback,
)
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

async def get_logs_container():
    return get_query_logs_container()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LogSummary(BaseModel):
    id: str
    session_id: str
    ip: str
    ip_type: Optional[str] = None
    user_agent: str
    question: str
    answer: str
    language: str
    model: str
    intent: str
    feedback: Optional[str]
    timestamp: str


class LogDetail(LogSummary):
    context: Optional[str]
    tokens: Optional[dict]


class StatsResponse(BaseModel):
    total_logs: int
    thumbs_up: int
    thumbs_down: int
    unique_ips: int


class FeedbackRequest(BaseModel):
    feedback: str       # "up" | "down"
    session_id: str     # needed as partition key


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=StatsResponse)
async def admin_stats(container=Depends(get_logs_container)):
    return await get_stats(container)


@router.get("/logs", response_model=list[LogSummary])
async def admin_list_logs(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ip: Optional[str] = Query(default=None),
    intent: Optional[str] = Query(default=None),
    ip_type: Optional[str] = Query(default=None, description="internal | external"),
    feedback: Optional[str] = Query(default=None, description="up | down | none"),
    search: Optional[str] = Query(default=None, description="Search in question text"),
    container=Depends(get_logs_container),
):
    logs = await list_logs(
        container,
        limit=limit,
        offset=offset,
        ip_filter=ip,
        intent_filter=intent,
        ip_type_filter=ip_type,
        feedback_filter=feedback,
        search=search,
    )
    return logs


@router.get("/logs/{log_id}", response_model=LogDetail)
async def admin_get_log(
    log_id: str,
    session_id: str = Query(..., description="Required as Cosmos partition key"),
    container=Depends(get_logs_container),
):
    log = await get_log_by_id(container, log_id, session_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    return log


@router.post("/logs/{log_id}/feedback")
async def admin_set_feedback(
    log_id: str,
    body: FeedbackRequest,
    container=Depends(get_logs_container),
):
    if body.feedback not in ("up", "down"):
        raise HTTPException(status_code=400, detail="feedback must be 'up' or 'down'")
    await set_feedback(container, log_id, body.session_id, body.feedback)
    return {"status": "ok"}