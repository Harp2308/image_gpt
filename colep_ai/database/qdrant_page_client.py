import time

from qdrant_client import QdrantClient, models

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.indexing.embedder import EMBED_DIM

logger = get_logger(__name__)

PAGE_COLLECTION_NAME = "colep_page_based_chunks"
UPSERT_BATCH_SIZE = 50


def get_qdrant_client() -> QdrantClient:
    return QdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY.get_secret_value(),
        timeout=60,
    )


def ensure_page_collection(client: QdrantClient) -> None:
    if not client.collection_exists(PAGE_COLLECTION_NAME):
        client.create_collection(
            collection_name=PAGE_COLLECTION_NAME,
            vectors_config={
                "text_pt": models.VectorParams(
                    size=EMBED_DIM,
                    distance=models.Distance.COSINE,
                ),
                "text_en": models.VectorParams(
                    size=EMBED_DIM,
                    distance=models.Distance.COSINE,
                ),
                "image_desc": models.VectorParams(
                    size=EMBED_DIM,
                    distance=models.Distance.COSINE,
                ),
            },
        )
        logger.info("Created collection: %s", PAGE_COLLECTION_NAME)
    else:
        logger.info("Collection already exists: %s", PAGE_COLLECTION_NAME)


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
                    "Batch %d attempt %d failed: %s", batch_index, attempt + 1, e
                )
                if attempt == max_retries - 1:
                    failed_batches.append(batch_index)
                else:
                    time.sleep(2**attempt)

    if failed_batches:
        logger.error(
            "Failed batches: %s — ~%d points not indexed",
            failed_batches,
            len(failed_batches) * UPSERT_BATCH_SIZE,
        )

    return failed_batches