"""
api/routes/sessions.py

GET /sessions  — list all sessions, newest first.
"""

from fastapi import APIRouter, Depends,HTTPException
from pydantic import BaseModel

from colep_ai.api.dependencies import get_cosmos
from colep_ai.core.logger import get_logger
# from colep_ai.database.mongo_client import delete_sessions_by_user,list_sessions
from colep_ai.database.cosmos_client import delete_sessions_by_user,list_sessions, delete_session
logger = get_logger(__name__)
router = APIRouter(tags=["Sessions"])


class SessionMeta(BaseModel):
    session_id: str
    summary: str
    title: str  
    user_name: str
    created_at: str
    updated_at: str


@router.get("/sessions", response_model=list[SessionMeta])
async def get_sessions(cosmos_container=Depends(get_cosmos)):
    return await list_sessions(cosmos_container)

@router.delete("/sessions/user")
async def delete_user_sessions(
    user_id: str,
    user_name: str,
    collection=Depends(get_cosmos),
):
    count = await delete_sessions_by_user(collection, user_id, user_name)
    return {"deleted": count}

@router.delete("/sessions/clear-all")
async def clear_all_sessions(
    user_id: str,
    user_name: str,
    collection=Depends(get_cosmos),
):
    count = await delete_sessions_by_user(collection, user_id, user_name)
    return {"deleted": count}

@router.delete("/sessions/{session_id}")
async def delete_single_session(
    session_id: str,
    cosmos_container=Depends(get_cosmos),
):
    count = await delete_session(cosmos_container, session_id)
    if count == 0:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": count}