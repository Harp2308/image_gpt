"""
database/cosmos_client.py

Async Cosmos DB client for conversation history persistence.
Drop-in replacement for mongo_client.py — same public API, same method signatures.

Responsibilities:
- Create and ensure DB + container at startup (async)
- Create session document on first turn
- Append individual turn documents (one doc per message)
- Upsert the running summary on the session document
- Load full session on resume (turns + summary)
- List all sessions (cross-partition query)
- Delete session by session_id
- Delete all sessions by user_id + user_name

Schema
------
All documents share one container, partitioned by session_id.
This keeps session doc + all its turn docs co-located in one logical partition —
single-partition read on every load_session call.

Session document  (id = session_id):
    {
        "id": "<session_id>",
        "session_id": "<session_id>",          # partition key
        "doc_type": "session",
        "user_id": "<str>",
        "user_name": "<str>",
        "summary": "",
        "created_at": "<iso>",
        "updated_at": "<iso>",
    }

Turn document  (id = "<session_id>_<turn_number>_<role>"):
    {
        "id": "<session_id>_<turn_number>_<role>",
        "session_id": "<session_id>",          # partition key
        "doc_type": "turn",
        "turn_number": <int>,
        "role": "user" | "assistant",
        "content": "<str>",
        "timestamp": "<iso>",
    }

Capacity mode
-------------
offer_throughput is intentionally omitted from create_container_if_not_exists.
- Serverless accounts: passing offer_throughput raises BadRequest on older SDK versions.
- Provisioned accounts: omitting it inherits database-level throughput, which is fine.
If Colep's account is provisioned and you need explicit RU/s on the container,
add offer_throughput=400 (or desired value) to create_container_if_not_exists below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from azure.cosmos import PartitionKey, exceptions
from azure.cosmos.aio import CosmosClient as AsyncCosmosClient

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton — one client, one session, reused for the app lifetime.
# Avoids per-request aiohttp session construction/teardown overhead.
# ---------------------------------------------------------------------------

_client: AsyncCosmosClient | None = None


def _get_client() -> AsyncCosmosClient:
    global _client
    if _client is None:
        _client = AsyncCosmosClient(
            url=settings.COSMOS_URL,
            credential=settings.COSMOS_KEY.get_secret_value(),
        )
    return _client


def get_cosmos_container():
    """
    Returns the async container client from the singleton.
    Intended to be used as a FastAPI dependency via Depends().
    No async context manager needed — client lifecycle is managed at app startup/shutdown.
    """
    client = _get_client()
    return (
        client
        .get_database_client(settings.COSMOS_DB_NAME)
        .get_container_client(settings.COSMOS_CONTAINER_NAME)
    )


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

async def ensure_cosmos_resources() -> None:
    """
    Called once at application startup (async lifespan handler).
    Creates the database and container if they don't already exist.
    Safe to call on every startup — uses if-not-exists semantics.

    offer_throughput is omitted intentionally — see module docstring.
    """
    client = _get_client()
    db = await client.create_database_if_not_exists(id=settings.COSMOS_DB_NAME)
    await db.create_container_if_not_exists(
        id=settings.COSMOS_CONTAINER_NAME,
        partition_key=PartitionKey(path="/session_id"),
    )
    logger.info(
        f"Cosmos resources ready | db={settings.COSMOS_DB_NAME} "
        f"container={settings.COSMOS_CONTAINER_NAME}"
    )


async def close_cosmos_client() -> None:
    """
    Called at application shutdown (async lifespan handler).
    Cleanly closes the underlying aiohttp session.
    Without this, you get ResourceWarning on shutdown.
    """
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("Cosmos client closed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

async def update_title(container, session_id: str, title: str) -> None:
    patch_ops = [
        {"op": "set", "path": "/title", "value": title},
        {"op": "set", "path": "/updated_at", "value": _now_iso()},
    ]
    await container.patch_item(
        item=session_id,
        partition_key=session_id,
        patch_operations=patch_ops,
    )
    logger.debug(f"Title set | session_id={session_id} title={title}")

# ---------------------------------------------------------------------------
# Session operations
# ---------------------------------------------------------------------------

async def create_session(
    container,
    session_id: str,
    user_id: str = "",
    user_name: str = "",
) -> None:
    """
    Creates the session root document.
    Called once when a new session_id is first seen.
    Silently ignores duplicate — safe under concurrent first-message race.
    """
    doc = {
        "id": session_id,
        "session_id": session_id,
        "doc_type": "session",
        "user_id": user_id,
        "user_name": user_name,
        "summary": "",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    try:
        await container.create_item(body=doc)
        logger.info(f"Session created | session_id={session_id} user_id={user_id}")
    except exceptions.CosmosResourceExistsError:
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
    upsert_item handles retry-safe re-delivery without duplicates.
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
    Patches the running summary on the session root document.
    Called from background task after every context eviction.
    patch_item is a partial update — does not rewrite the full document.
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
    logger.debug(f"Summary updated | session_id={session_id}")


async def load_session(container, session_id: str) -> Optional[dict]:
    """
    Loads the full session on resume:
    - Session root document (summary, user_id, user_name)
    - All turn documents ordered by turn_number ASC

    Both reads hit a single logical partition (session_id) — no fan-out.

    Returns:
        {
            "summary": str,
            "user_id": str,
            "user_name": str,
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
        logger.info(f"Session not found | session_id={session_id}")
        return None

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
        partition_key=session_id,   # single-partition query — no cross-partition fan-out
    ):
        turns.append({
            "turn_number": item["turn_number"],
            "role": item["role"],
            "content": item["content"],
        })

    logger.info(
        f"Session loaded | session_id={session_id} "
        f"turns={len(turns)} has_summary={bool(session_doc.get('summary'))}"
    )

    return {
        "summary": session_doc.get("summary", ""),
        "user_id": session_doc.get("user_id", ""),
        "user_name": session_doc.get("user_name", ""),
        "turns": turns,
    }


async def list_sessions(container, limit: int = 100) -> list[dict]:
    """
    Returns all session documents sorted by updated_at descending.

    ⚠ Cross-partition query — queries doc_type = 'session' across all partitions.
    At pilot scale (50 users, hundreds of sessions) this is perfectly acceptable.
    At large scale, consider maintaining a separate 'sessions index' container
    with a fixed partition key (e.g. "all") to avoid fan-out reads.

    Cross-partition queries are enabled by default in newer SDK versions —
    enable_cross_partition_query parameter has been removed/deprecated.
    """
    query = (
        "SELECT c.id, c.summary,c.title, c.user_id, c.user_name, c.created_at, c.updated_at "
        "FROM c "
        "WHERE c.doc_type = 'session' "
        "ORDER BY c.updated_at DESC "
        f"OFFSET 0 LIMIT {limit}"
    )

    sessions = []
    async for item in container.query_items(
        query=query,
    ):
        sessions.append({
            "session_id": item["id"],
            "summary": item.get("summary", ""),
            "title": item.get("title", ""),
            "user_id": item.get("user_id", ""),
            "user_name": item.get("user_name", ""),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
        })

    return sessions


async def delete_session(container, session_id: str) -> int:
    """
    Deletes the session root document and all its turn documents.
    All deletes are within the same partition — no fan-out.
    partition_key is mandatory on every delete_item call in Cosmos.
    """
    deleted = 0

    # Delete session root
    try:
        await container.delete_item(item=session_id, partition_key=session_id)
        deleted += 1
    except exceptions.CosmosResourceNotFoundError:
        pass

    # Collect and delete all turns for this session
    query = (
        "SELECT c.id FROM c "
        "WHERE c.session_id = @session_id AND c.doc_type = 'turn'"
    )
    params = [{"name": "@session_id", "value": session_id}]

    async for item in container.query_items(
        query=query,
        parameters=params,
        partition_key=session_id,
    ):
        try:
            await container.delete_item(item=item["id"], partition_key=session_id)
            deleted += 1
        except exceptions.CosmosResourceNotFoundError:
            pass

    logger.info(f"Deleted session | session_id={session_id} | count={deleted}")
    return deleted


async def delete_sessions_by_user(container, user_id: str, user_name: str) -> int:
    """
    Deletes all sessions and their turns for a given user_id + user_name.

    Two-phase:
    1. Cross-partition query to find all session_ids for this user.
    2. Per session_id: delete session root + all turns (single-partition each).

    Cross-partition query in phase 1 is unavoidable given the partition key
    is session_id, not user_id. Acceptable at pilot scale.
    """
    # Phase 1 — find all session_ids for this user (cross-partition)
    query = (
        "SELECT c.id, c.session_id FROM c "
        "WHERE c.doc_type = 'session' "
        "AND c.user_id = @user_id "
        "AND c.user_name = @user_name"
    )
    params = [
        {"name": "@user_id", "value": user_id},
        {"name": "@user_name", "value": user_name},
    ]

    session_ids = []
    async for item in container.query_items(
        query=query,
        parameters=params,
    ):
        session_ids.append(item["session_id"])

    if not session_ids:
        logger.info(f"No sessions found | user_id={user_id} user_name={user_name}")
        return 0

    # Phase 2 — delete each session and its turns (single-partition per session)
    total = 0
    for session_id in session_ids:
        total += await delete_session(container, session_id)

    logger.info(
        f"Deleted user sessions | user_id={user_id} user_name={user_name} | count={total}"
    )
    return total