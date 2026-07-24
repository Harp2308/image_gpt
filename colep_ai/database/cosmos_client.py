"""
database/cosmos_client.py

Cosmos DB client for conversation history persistence.

Responsibilities:
- Create session document on first turn
- Append individual turn documents (one doc per message)
- Upsert the running summary on the session document
- Load full session on resume (turns + summary)

Schema
------
Each Cosmos document has a partition key of `session_id`.

Session document  (id = session_id):
    {
        "id": "<session_id>",
        "session_id": "<session_id>",          # partition key
        "doc_type": "session",
        "summary": "",                          # updated incrementally
        "created_at": "<iso>",
        "updated_at": "<iso>",
    }

Turn document  (id = "<session_id>_<turn_number>_<role>"):
    {
        "id": "<session_id>_<turn>_<role>",
        "session_id": "<session_id>",          # partition key
        "doc_type": "turn",
        "turn_number": <int>,
        "role": "user" | "assistant",
        "content": "<str>",
        "timestamp": "<iso>",
    }

All documents share the same container, partitioned by session_id.
This keeps all docs for a session co-located — single logical partition
read for any session load.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from azure.cosmos import CosmosClient, PartitionKey, exceptions
from azure.cosmos.aio import CosmosClient as AsyncCosmosClient

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Synchronous client (used in FastAPI background tasks + startup)
# ---------------------------------------------------------------------------

def _get_sync_client() -> CosmosClient:
    return CosmosClient(
        url=settings.COSMOS_URL,
        credential=settings.COSMOS_KEY.get_secret_value(),
    )


def ensure_cosmos_resources() -> None:
    """
    Called once at application startup.
    Creates the database and container if they don't already exist.
    Safe to call on every startup — uses if-not-exists semantics.
    """
    client = _get_sync_client()
    db = client.create_database_if_not_exists(id=settings.COSMOS_DB_NAME)
    db.create_container_if_not_exists(
        id=settings.COSMOS_CONTAINER_NAME,
        partition_key=PartitionKey(path="/session_id"),
        offer_throughput=400,   # minimum RU/s — sufficient for pilot volume
    )
    logger.info(
        f"Cosmos resources ready | db={settings.COSMOS_DB_NAME} "
        f"container={settings.COSMOS_CONTAINER_NAME}"
    )


# ---------------------------------------------------------------------------
# Async client factory (used in FastAPI route handlers)
# ---------------------------------------------------------------------------

def get_cosmos_container():
    """
    Returns an async Cosmos container client.
    Intended to be used as a FastAPI dependency via Depends().

    Usage in dependency:
        container = get_cosmos_container()
        async with container:
            ...

    Note: CosmosClient is lightweight to construct — no persistent
    connection pool to manage. Safe to construct per-request.
    """
    client = AsyncCosmosClient(
        url=settings.COSMOS_URL,
        credential=settings.COSMOS_KEY.get_secret_value(),
    )
    return client.get_database_client(settings.COSMOS_DB_NAME).get_container_client(
        settings.COSMOS_CONTAINER_NAME
    )


# ---------------------------------------------------------------------------
# Session operations
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_session(container, session_id: str) -> None:
    """
    Creates the session root document.
    Called once when a new session_id is first seen.
    """
    doc = {
        "id": session_id,
        "session_id": session_id,
        "doc_type": "session",
        "summary": "",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    try:
        await container.create_item(body=doc)
        logger.info(f"Session created | session_id={session_id}")
    except exceptions.CosmosResourceExistsError:
        # Race condition on first message — safe to ignore
        logger.debug(f"Session already exists | session_id={session_id}")


async def append_turn(
    container,
    session_id: str,
    turn_number: int,
    role: str,          # "user" | "assistant"
    content: str,
) -> None:
    """
    Persists a single message (one side of a turn) to Cosmos.
    id is composite to guarantee uniqueness within the partition.
    """
    doc = {
        "id": f"{session_id}_{turn_number}_{role}",
        "session_id": session_id,
        "doc_type": "turn",
        "turn_number": turn_number,
        "role": role,
        "content": content,
        "timestamp": _now_iso(),
    }
    await container.upsert_item(body=doc)
    logger.debug(
        f"Turn persisted | session_id={session_id} "
        f"turn={turn_number} role={role}"
    )


async def update_summary(
    container,
    session_id: str,
    summary: str,
) -> None:
    """
    Upserts the running summary on the session root document.
    Called from the background task after every eviction.
    """
    patch_ops = [
        {"op": "set", "path": "/summary", "value": summary},
        {"op": "set", "path": "/updated_at", "value": _now_iso()},
    ]
    await container.patch_item(
        item=session_id,
        partition_key=session_id,
        patch_operations=patch_ops,
    )
    logger.debug(f"Summary updated in Cosmos | session_id={session_id}")


async def load_session(container, session_id: str) -> Optional[dict]:
    """
    Loads the full session on resume:
    - Session root document (contains summary)
    - All turn documents ordered by turn_number ASC

    Returns:
        {
            "summary": str,
            "turns": [
                {"turn_number": int, "role": str, "content": str},
                ...
            ]
        }
    or None if session does not exist.
    """
    try:
        session_doc = await container.read_item(
            item=session_id,
            partition_key=session_id,
        )
    except exceptions.CosmosResourceNotFoundError:
        logger.info(f"Session not found in Cosmos | session_id={session_id}")
        return None

    # Query all turn documents for this session, ordered by turn_number
    query = (
        "SELECT c.turn_number, c.role, c.content "
        "FROM c "
        "WHERE c.session_id = @session_id AND c.doc_type = 'turn' "
        "ORDER BY c.turn_number ASC"
    )
    params = [{"name": "@session_id", "value": session_id}]

    turns = []
    async for item in container.query_items(
        query=query,
        parameters=params,
        partition_key=session_id,
    ):
        turns.append({
            "turn_number": item["turn_number"],
            "role": item["role"],
            "content": item["content"],
        })

    logger.info(
        f"Session loaded from Cosmos | session_id={session_id} "
        f"turns={len(turns)} has_summary={bool(session_doc.get('summary'))}"
    )

    return {
        "summary": session_doc.get("summary", ""),
        "turns": turns,
    }