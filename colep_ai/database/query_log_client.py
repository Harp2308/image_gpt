"""
database/query_log_client.py

Motor (async MongoDB) client for query_logs collection.
Mirrors the pattern in mongo_client.py exactly.

Collection: query_logs (in same DB as chat_sessions)

Document schema:
    {
        "_id":        "<uuid>",
        "session_id": "<str>",
        "ip":         "<str>",
        "user_agent": "<str>",
        "question":   "<str>",
        "answer":     "<str>",
        "language":   "pt | en | ''",
        "model":      "<str>",
        "context":    "<str | null>",
        "intent":     "greeting | retrieval | malicious | conversation_summary",
        "feedback":   null | "up" | "down",
        "timestamp":  "<iso>",
    }
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

_client: AsyncIOMotorClient | None = None


def _get_collection():
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.COSMOS_URL)
    return _client[settings.COSMOS_DB_NAME]["query_logs"]


def get_query_logs_container():
    """Returns the Motor collection. Named 'container' to match existing call sites."""
    return _get_collection()


async def ensure_query_logs_container() -> None:
    """
    Creates indexes on query_logs collection.
    Call at startup alongside ensure_cosmos_resources().
    """
    col = _get_collection()
    await col.create_index([("timestamp", -1)])
    await col.create_index([("session_id", 1)])
    await col.create_index([("ip", 1)])
    await col.create_index([("intent", 1)])
    await col.create_index([("feedback", 1)])
    logger.info(
        f"query_logs collection ready | db={settings.COSMOS_DB_NAME}"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

async def write_query_log(
    container,
    session_id: str,
    ip: str,
    user_agent: str,
    question: str,
    answer: str,
    intent: str,
    language: str = "",
    model: str = "",
    context: Optional[str] = None,
) -> str:
    log_id = str(uuid.uuid4())
    doc = {
        "_id": log_id,
        "session_id": session_id,
        "ip": ip,
        "user_agent": user_agent,
        "question": question,
        "answer": answer,
        "language": language,
        "model": model,
        "context": context,
        "intent": intent,
        "feedback": None,
        "timestamp": _now_iso(),
    }
    await container.insert_one(doc)
    logger.debug(f"Query log written | id={log_id} | intent={intent} | session={session_id}")
    return log_id


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

async def set_feedback(container, log_id: str, session_id: str, feedback: str) -> None:
    await container.update_one(
        {"_id": log_id},
        {"$set": {"feedback": feedback}},
    )
    logger.debug(f"Feedback set | id={log_id} | feedback={feedback}")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_log_by_id(container, log_id: str, session_id: str) -> Optional[dict]:
    doc = await container.find_one({"_id": log_id})
    if not doc:
        return None
    doc["id"] = doc.pop("_id")
    return doc


async def list_logs(
    container,
    limit: int = 50,
    offset: int = 0,
    ip_filter: Optional[str] = None,
    intent_filter: Optional[str] = None,
    ip_type_filter: Optional[str] = None,
    feedback_filter: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    query = {}

    if ip_filter:
        query["ip"] = ip_filter
    if intent_filter:
        query["intent"] = intent_filter
    if ip_type_filter:
        query["ip_type"] = ip_type_filter
    if feedback_filter == "none":
        query["feedback"] = None
    elif feedback_filter in ("up", "down"):
        query["feedback"] = feedback_filter
    if search:
        query["question"] = {"$regex": search, "$options": "i"}

    # Exclude context from list view
    projection = {
        "context": 0,
    }

    cursor = (
        container.find(query, projection)
        .sort("timestamp", -1)
        .skip(offset)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)

    # Rename _id → id for Pydantic
    for doc in docs:
        doc["id"] = doc.pop("_id")

    return docs


async def get_stats(container) -> dict:
    total      = await container.count_documents({})
    thumbs_up  = await container.count_documents({"feedback": "up"})
    thumbs_down = await container.count_documents({"feedback": "down"})
    unique_ips = len(await container.distinct("ip"))

    return {
        "total_logs":   total,
        "thumbs_up":    thumbs_up,
        "thumbs_down":  thumbs_down,
        "unique_ips":   unique_ips,
    }