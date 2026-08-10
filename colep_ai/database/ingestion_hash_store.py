"""
database/ingestion_hash_store.py

Sync Cosmos client for ingestion deduplication via PDF/Excel content hashing.

Responsibilities:
- Ensure `ingested_files` container exists (called at worker startup)
- Check if a file has already been ingested with the same content hash
- Upsert hash record after successful ingestion

Why sync (not async):
- Used inside Celery tasks which run in a sync context
- asyncio.run() inside Celery is an anti-pattern — creates/destroys event loop per call
- Mirrors get_claude_client_sync pattern already established in this codebase

Container: ingested_files
Partition key: /source_file
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from azure.cosmos import CosmosClient, PartitionKey, exceptions

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

_INGESTED_FILES_CONTAINER = "ingested_files"

_sync_client: CosmosClient | None = None


def _get_sync_client() -> CosmosClient:
    global _sync_client
    if _sync_client is None:
        _sync_client = CosmosClient(
            url=settings.COSMOS_URL,
            credential=settings.COSMOS_KEY.get_secret_value(),
        )
    return _sync_client


def _get_container():
    client = _get_sync_client()
    db = client.get_database_client(settings.COSMOS_DB_NAME)
    return db.get_container_client(_INGESTED_FILES_CONTAINER)


def ensure_ingested_files_container() -> None:
    client = _get_sync_client()
    db = client.create_database_if_not_exists(id=settings.COSMOS_DB_NAME)
    try:
        db.create_container(
            id=_INGESTED_FILES_CONTAINER,
            partition_key=PartitionKey(path="/source_file"),
        )
        logger.info(f"Cosmos container created | {_INGESTED_FILES_CONTAINER}")
    except exceptions.CosmosResourceExistsError:
        logger.info(f"Cosmos container already exists | {_INGESTED_FILES_CONTAINER}")

def compute_file_hash(path: str | Path) -> str:
    """SHA-256 of file contents. Reads in 64KB chunks — safe for large files."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_already_ingested(source_file: str, current_hash: str) -> bool:
    """
    Returns True if source_file exists in Cosmos with the same content hash.
    Returns False if:
      - No record exists (new file)
      - Record exists but hash differs (file updated)
    """
    container = _get_container()
    try:
        doc = container.read_item(item=source_file, partition_key=source_file)
        stored_hash = doc.get("pdf_hash")
        if stored_hash == current_hash:
            logger.info(f"Hash match — skipping ingestion | source_file={source_file}")
            return True
        logger.info(
            f"Hash mismatch — reprocessing | source_file={source_file} "
            f"stored={stored_hash[:8]}... current={current_hash[:8]}..."
        )
        return False
    except exceptions.CosmosResourceNotFoundError:
        logger.info(f"No hash record found — new file | source_file={source_file}")
        return False


def upsert_ingestion_hash(
    source_file: str,
    folder_name: str,
    file_hash: str,
) -> None:
    """
    Upserts the hash record for source_file.
    Call after successful ingestion only — not before.
    """
    container = _get_container()
    doc = {
        "id": source_file,
        "source_file": source_file,
        "folder_name": folder_name,
        "pdf_hash": file_hash,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    container.upsert_item(body=doc)
    logger.info(f"Hash record upserted | source_file={source_file}")
