"""
database/mongo_client.py

Local MongoDB client — drop-in replacement for cosmos_client.py.
Same public API, same method signatures.
Swap back to cosmos_client.py when Colep credentials are available.

Database : colep_ai
Collection: chat_sessions

Document schema
---------------
Session document  (one per session):
    {
        "_id": "<session_id>",
        "doc_type": "session",
        "summary": "",
        "created_at": "<iso>",
        "updated_at": "<iso>",
    }

Turn document  (one per message):
    {
        "_id": "<session_id>_<turn_number>_<role>",
        "doc_type": "turn",
        "session_id": "<session_id>",
        "turn_number": <int>,
        "role": "user" | "assistant",
        "content": "<str>",
        "timestamp": "<iso>",
    }
"""

from __future__ import annotations

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
    return _client[settings.COSMOS_DB_NAME][settings.COSMOS_CONTAINER_NAME]


def get_cosmos_container():
    """Matches cosmos_client.py public API — returns the collection."""
    return _get_collection()


def ensure_cosmos_resources() -> None:
    """
    MongoDB creates collections on first write — nothing to do here.
    Kept for API compatibility with cosmos_client.py.
    """
    logger.info(
        f"MongoDB ready | db={settings.COSMOS_DB_NAME} | "
        f"collection={settings.COSMOS_CONTAINER_NAME}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Session operations  (same signatures as cosmos_client.py)
# ---------------------------------------------------------------------------

async def create_session(
    collection,
    session_id: str,
    user_id: str = "",
    user_name: str = "",
) -> None:
    doc = {
        "_id": session_id,
        "doc_type": "session",
        "user_id": user_id,
        "user_name": user_name,
        "summary": "",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    try:
        await collection.insert_one(doc)
        logger.info(f"Session created | session_id={session_id} user_id={user_id}")
    except Exception:
        # Duplicate key — session already exists, safe to ignore
        logger.debug(f"Session already exists | session_id={session_id}")


async def append_turn(
    collection,
    session_id: str,
    turn_number: int,
    role: str,
    content: str,
) -> None:
    doc = {
        "_id": f"{session_id}_{turn_number}_{role}",
        "doc_type": "turn",
        "session_id": session_id,
        "turn_number": turn_number,
        "role": role,
        "content": content,
        "timestamp": _now_iso(),
    }
    await collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    logger.debug(f"Turn persisted | session_id={session_id} turn={turn_number} role={role}")


async def update_summary(collection, session_id: str, summary: str) -> None:
    await collection.update_one(
        {"_id": session_id, "doc_type": "session"},
        {"$set": {"summary": summary, "updated_at": _now_iso()}},
    )
    logger.debug(f"Summary updated in MongoDB | session_id={session_id}")


async def load_session(collection, session_id: str) -> Optional[dict]:
    session_doc = await collection.find_one({"_id": session_id, "doc_type": "session"})
    if not session_doc:
        logger.info(f"Session not found in MongoDB | session_id={session_id}")
        return None

    turns = await collection.find(
        {"session_id": session_id, "doc_type": "turn"},
        sort=[("turn_number", 1)],
    ).to_list(length=None)

    logger.info(
        f"Session loaded from MongoDB | session_id={session_id} "
        f"turns={len(turns)} has_summary={bool(session_doc.get('summary'))}"
    )

    return {
        "summary": session_doc.get("summary", ""),
        "user_id": session_doc.get("user_id", ""),
        "user_name": session_doc.get("user_name", ""),
        "turns": [
            {"turn_number": t["turn_number"], "role": t["role"], "content": t["content"]}
            for t in turns
        ],
    }


async def list_sessions(collection, limit: int = 100) -> list[dict]:
    """Return all sessions sorted by updated_at descending."""
    cursor = collection.find(
        {"doc_type": "session"},
        {"_id": 1, "summary": 1, "user_name": 1, "created_at": 1, "updated_at": 1},
        sort=[("updated_at", -1)],
    ).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [
        {
            "session_id": d["_id"],
            "summary": d.get("summary", ""),
            "user_name": d.get("user_name", ""),
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
        }
        for d in docs
    ]


async def delete_session(collection, session_id: str) -> int:
    """Delete session and all its turns."""
    result = await collection.delete_many({"session_id": session_id})
    logger.info(f"Deleted session | session_id={session_id} | count={result.deleted_count}")
    return result.deleted_count