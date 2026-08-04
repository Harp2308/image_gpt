"""
database/query_log_client.py

Async Cosmos DB client for query_logs persistence.
Drop-in replacement for the Motor/MongoDB version — same public API, same method signatures.

Container : query_logs  (separate container from chat_sessions)
Partition : session_id  — most natural unit of work; all cross-cutting
            reads (stats, list, search) are cross-partition regardless
            of what key is chosen at pilot scale (50 users, ~200 queries/day).

Document schema:
    {
        "id":         "<uuid>",                  # Cosmos id (was _id in Mongo)
        "session_id": "<str>",                   # partition key
        "ip":         "<str>",
        "ip_type":    "internal | external | unknown",
        "user_agent": "<str>",
        "question":   "<str>",
        "answer":     "<str>",
        "language":   "pt | en | ''",
        "model":      "<str>",
        "context":    "<str | null>",
        "intent":     "greeting | retrieval | malicious | conversation_summary",
        "feedback":   null | "up" | "down",
        "tokens": {
            "classifier_input":  <int | null>,
            "classifier_output": <int | null>,
            "generation_input":  <int | null>,
            "generation_output": <int | null>,
        },
        "timestamp":  "<iso>",
    }

Query differences vs MongoDB
-----------------------------
- $regex search        → CONTAINS(LOWER(c.question), LOWER(@search))  [substring only, no regex]
- count_documents      → SELECT VALUE COUNT(1) FROM c WHERE ...        [full scan — acceptable at pilot scale]
- distinct("ip")       → SELECT c.ip FROM c  + Python-side dedup       [Cosmos has no DISTINCT aggregate]
- feedback = None      → IS_NULL(c.feedback)                           [Cosmos SQL null check syntax]
- Dynamic filters      → parameterized SQL string built at runtime
- skip + limit         → OFFSET @offset LIMIT @limit                   [Cosmos native, expensive at high offsets]
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from azure.cosmos import PartitionKey, exceptions
from azure.cosmos.aio import CosmosClient as AsyncCosmosClient

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton — shared with cosmos_client.py pattern.
# query_logs uses a SEPARATE container but the SAME Cosmos account/client.
# We maintain our own singleton here to keep modules independent.
# ---------------------------------------------------------------------------

_client: AsyncCosmosClient | None = None

_QUERY_LOGS_CONTAINER = "query_logs"


def _get_client() -> AsyncCosmosClient:
    global _client
    if _client is None:
        _client = AsyncCosmosClient(
            url=settings.COSMOS_URL,
            credential=settings.COSMOS_KEY.get_secret_value(),
        )
    return _client


def get_query_logs_container():
    """
    Returns the async Cosmos container client for query_logs.
    Called directly (not via Depends) — matches existing call-site pattern.
    Client lifecycle managed at app startup/shutdown.
    """
    client = _get_client()
    return (
        client
        .get_database_client(settings.COSMOS_DB_NAME)
        .get_container_client(_QUERY_LOGS_CONTAINER)
    )


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

async def ensure_query_logs_container() -> None:
    """
    Called once at application startup.
    Creates the query_logs container if it doesn't already exist.
    offer_throughput omitted — serverless account; see cosmos_client.py note.
    """
    client = _get_client()
    db = await client.create_database_if_not_exists(id=settings.COSMOS_DB_NAME)
    await db.create_container_if_not_exists(
        id=_QUERY_LOGS_CONTAINER,
        partition_key=PartitionKey(path="/session_id"),
    )
    logger.info(
        f"query_logs container ready | db={settings.COSMOS_DB_NAME} "
        f"container={_QUERY_LOGS_CONTAINER}"
    )


async def close_query_logs_client() -> None:
    """
    Called at application shutdown.
    Closes the underlying aiohttp session to avoid ResourceWarning.
    """
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("query_logs Cosmos client closed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify_ip(ip: str) -> str:
    if ip in ("127.0.0.1", "::1", "localhost"):
        return "internal"
    try:
        first_octet = int(ip.split(".")[0])
        return "internal" if first_octet in (10, 11, 12, 13, 192, 172) else "external"
    except (ValueError, IndexError):
        return "unknown"


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
    tokens: Optional[dict] = None,
    classifier_response: Optional[dict] = None,
) -> str:
    log_id = str(uuid.uuid4())
    doc = {
        "id": log_id,                        # Cosmos uses 'id', not '_id'
        "session_id": session_id,            # partition key
        "ip": ip,
        "ip_type": _classify_ip(ip),
        "user_agent": user_agent,
        "question": question,
        "answer": answer,
        "language": language,
        "model": model,
        "context": context,
        "intent": intent,
        "feedback": None,
        "classifier_response": classifier_response,
        "tokens": tokens or {
            "classifier_input":  None,
            "classifier_output": None,
            "generation_input":  None,
            "generation_output": None,
        },
        "timestamp": _now_iso(),
    }
    await container.create_item(body=doc)
    logger.debug(
        f"Query log written | id={log_id} | intent={intent} | session={session_id}"
    )
    return log_id


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

async def set_feedback(container, log_id: str, session_id: str, feedback: str) -> None:
    """
    Patches feedback on a query log document.
    session_id is the partition key — required for point operations in Cosmos.
    patch_item does a partial update; no full document rewrite.
    """
    patch_ops = [
        {"op": "set", "path": "/feedback", "value": feedback},
    ]
    try:
        await container.patch_item(
            item=log_id,
            partition_key=session_id,
            patch_operations=patch_ops,
        )
        logger.debug(f"Feedback set | id={log_id} | feedback={feedback}")
    except exceptions.CosmosResourceNotFoundError:
        logger.warning(f"Feedback update failed — log not found | id={log_id}")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_log_by_id(container, log_id: str, session_id: str) -> Optional[dict]:
    """
    Point read by log_id + session_id (partition key).
    session_id is required — without it Cosmos does a cross-partition scan.
    Callers must pass session_id; it's already in the existing signature.
    """
    try:
        doc = await container.read_item(item=log_id, partition_key=session_id)
        doc["id"] = doc["id"]   # already 'id' in Cosmos — no rename needed
        return doc
    except exceptions.CosmosResourceNotFoundError:
        return None


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
    """
    Cross-partition query with dynamic filters.

    Filter translation from MongoDB:
    - ip_filter        → c.ip = @ip
    - intent_filter    → c.intent = @intent
    - ip_type_filter   → c.ip_type = @ip_type
    - feedback = None  → IS_NULL(c.feedback)      [cannot use = null in Cosmos SQL]
    - feedback up/down → c.feedback = @feedback
    - search (regex)   → CONTAINS(LOWER(c.question), LOWER(@search))

    context field excluded from list view — same as Mongo projection.
    OFFSET/LIMIT is native in Cosmos SQL but expensive at high offsets.
    Acceptable at pilot scale.
    """
    where_clauses = []
    params = []

    if ip_filter:
        where_clauses.append("c.ip = @ip")
        params.append({"name": "@ip", "value": ip_filter})

    if intent_filter:
        where_clauses.append("c.intent = @intent")
        params.append({"name": "@intent", "value": intent_filter})

    if ip_type_filter:
        where_clauses.append("c.ip_type = @ip_type")
        params.append({"name": "@ip_type", "value": ip_type_filter})

    if feedback_filter == "none":
        where_clauses.append("IS_NULL(c.feedback)")
    elif feedback_filter in ("up", "down"):
        where_clauses.append("c.feedback = @feedback")
        params.append({"name": "@feedback", "value": feedback_filter})

    if search:
        where_clauses.append("CONTAINS(LOWER(c.question), LOWER(@search))")
        params.append({"name": "@search", "value": search})

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    query = (
        "SELECT c.id, c.session_id, c.ip, c.ip_type, c.user_agent, "
        "c.question, c.answer, c.language, c.model, c.intent, "
        "c.feedback, c.tokens, c.timestamp "
        f"FROM c {where_sql} "
        "ORDER BY c.timestamp DESC "
        f"OFFSET {offset} LIMIT {limit}"
    )

    docs = []
    async for item in container.query_items(
        query=query,
        parameters=params if params else None,
    ):
        docs.append(item)

    return docs


async def get_stats(container) -> dict:
    """
    Aggregation stats across all query logs.

    MongoDB → Cosmos translation:
    - count_documents({})           → SELECT VALUE COUNT(1) FROM c
    - count_documents(feedback=up)  → SELECT VALUE COUNT(1) FROM c WHERE c.feedback = 'up'
    - count_documents(feedback=down)→ SELECT VALUE COUNT(1) FROM c WHERE c.feedback = 'down'
    - distinct("ip")                → SELECT c.ip FROM c  +  Python set dedup
                                      (Cosmos has no DISTINCT aggregate)

    All are cross-partition full scans — acceptable at pilot scale.
    At production scale, maintain a separate stats/counter document
    updated incrementally on each write_query_log call.
    """

    async def _count(where: str = "", params: list = None) -> int:
        q = f"SELECT VALUE COUNT(1) FROM c {where}"
        result = []
        async for item in container.query_items(
            query=q,
            parameters=params or [],
        ):
            result.append(item)
        return result[0] if result else 0

    total       = await _count()
    thumbs_up   = await _count("WHERE c.feedback = @f", [{"name": "@f", "value": "up"}])
    thumbs_down = await _count("WHERE c.feedback = @f", [{"name": "@f", "value": "down"}])

    # Cosmos has no DISTINCT aggregate — pull ip field only, dedup in Python
    ip_query = "SELECT c.ip FROM c"
    ips = set()
    async for item in container.query_items(
        query=ip_query,
    ):
        if item.get("ip"):
            ips.add(item["ip"])

    return {
        "total_logs":  total,
        "thumbs_up":   thumbs_up,
        "thumbs_down": thumbs_down,
        "unique_ips":  len(ips),
    }