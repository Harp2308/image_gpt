from qdrant_client import QdrantClient

from colep_ai.database.qdrant_db_client import get_qdrant_client, COLLECTION_NAME
from colep_ai.indexing.embedder import get_openai_client, embed_texts
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)


def search(
    query: str,
    top_k: int = 5,
    source_file: str | None = None,
    page_number: int | None = None,
) -> list[dict]:
    openai_client = get_openai_client()
    query_vector = embed_texts(openai_client, [query])[0]

    qdrant_filter = None
    conditions = []
    if source_file:
        conditions.append({"key": "source_file", "match": {"value": source_file}})
    if page_number is not None:
        conditions.append({"key": "page_number", "match": {"value": page_number}})
    if conditions:
        qdrant_filter = {"must": conditions}

    client = get_qdrant_client()
    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=top_k,
        query_filter=qdrant_filter,
        with_payload=True,
    ).points

    logger.info(f"Query='{query}' returned {len(results)} results")

    return [
        {
            "score": r.score,
            "entry_id": r.payload.get("entry_id"),
            "entry_text": r.payload.get("entry_text"),
            "entry_text_en": r.payload.get("entry_text_en"),
            "image_ids": r.payload.get("image_ids", []),
            "image_description": r.payload.get("image_description"),
            "source_file": r.payload.get("source_file"),
            "page_number": r.payload.get("page_number"),
            "document_title": r.payload.get("document_title"),
            "document_code": r.payload.get("document_code"),
        }
        for r in results
    ]