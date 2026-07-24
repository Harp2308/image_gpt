"""
api/routes/sessions.py

GET /sessions  — list all sessions, newest first.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from colep_ai.api.dependencies import get_cosmos
from colep_ai.database.mongo_client import list_sessions
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["Sessions"])


class SessionMeta(BaseModel):
    session_id: str
    summary: str
    user_name: str
    created_at: str
    updated_at: str


@router.get("/sessions", response_model=list[SessionMeta])
async def get_sessions(cosmos_container=Depends(get_cosmos)):
    return await list_sessions(cosmos_container)