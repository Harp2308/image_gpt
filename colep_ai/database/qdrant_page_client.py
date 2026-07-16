import time

from qdrant_client import QdrantClient, models

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.indexing.embedder import EMBED_DIM

logger = get_logger(__name__)

PAGE_COLLECTION_NAME = "colep_page_based_chunks_line"
# PAGE_COLLECTION_NAME = "colep_page_based_chunks_p"

UPSERT_BATCH_SIZE = 50


def get_qdrant_client() -> QdrantClient:
    return QdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY.get_secret_value(),
        timeout=60,
    )
# v2 -----

def ensure_page_collection(client: QdrantClient) -> None:
    if not client.collection_exists(PAGE_COLLECTION_NAME):
        client.create_collection(
            collection_name=PAGE_COLLECTION_NAME,
            vectors_config={
                "text_pt": models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
                "text_en": models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
                "image_desc": models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
            },
        )
        logger.info(f"Created collection: {PAGE_COLLECTION_NAME}")
    else:
        logger.info(f"Collection already exists: {PAGE_COLLECTION_NAME}")

    create_indexes(client)  # idempotent — safe to call every time


def create_indexes(client: QdrantClient) -> None:
    keyword_fields = [
        ("document_code", models.PayloadSchemaType.KEYWORD),
        ("source_file", models.PayloadSchemaType.KEYWORD),
    ]
    integer_fields = [
        ("page_number", models.PayloadSchemaType.INTEGER),
        ("line_number", models.PayloadSchemaType.INTEGER),  # added
    ]
    fulltext_fields = ["text_pt", "text_en"]  # for BM25

    for field_name, schema in keyword_fields + integer_fields:
        try:
            client.create_payload_index(
                collection_name=PAGE_COLLECTION_NAME,
                field_name=field_name,
                field_schema=schema,
            )
        except Exception as e:
            logger.debug(f"Index on '{field_name}' already exists or failed: {e}")

    for field_name in fulltext_fields:
        try:
            client.create_payload_index(
                collection_name=PAGE_COLLECTION_NAME,
                field_name=field_name,
                field_schema=models.TextIndexParams(
                    type=models.TextIndexType.TEXT,
                    tokenizer=models.TokenizerType.WORD,
                    min_token_len=2,
                    max_token_len=30,
                    lowercase=True,
                ),
            )
        except Exception as e:
            logger.debug(f"Full-text index on '{field_name}' already exists or failed: {e}")

# ----------------

def upsert_page_chunks(
    client: QdrantClient,
    chunks: list[dict],
    vectors_pt: list[list[float]],
    vectors_en: list[list[float]],
    vectors_img: list[list[float]],
    max_retries: int = 3,
) -> list[int]:
    points = [
        models.PointStruct(
            id=chunk["id"],
            vector={
                "text_pt": v_pt,
                "text_en": v_en,
                "image_desc": v_img,
            },
            payload=chunk["payload"],
        )
        for chunk, v_pt, v_en, v_img in zip(chunks, vectors_pt, vectors_en, vectors_img)
    ]

    failed_batches = []
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i : i + UPSERT_BATCH_SIZE]
        batch_index = i // UPSERT_BATCH_SIZE
        for attempt in range(max_retries):
            try:
                client.upsert(collection_name=PAGE_COLLECTION_NAME, points=batch)
                break
            except Exception as e:
                logger.warning(
                    f"Batch { batch_index} attempt {attempt + 1 } failed: {e}"
                )
                if attempt == max_retries - 1:
                    failed_batches.append(batch_index)
                else:
                    time.sleep(2**attempt)

    if failed_batches:
        logger.error(
            f"Failed batches: {failed_batches} — ~{len(failed_batches) * UPSERT_BATCH_SIZE} points not indexed",
          )

    return failed_batches