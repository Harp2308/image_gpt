from qdrant_client import models ,QdrantClient

from colep_ai.core.config import settings
from colep_ai.indexing.embedder import EMBED_DIM
from colep_ai.core.logger import get_logger

# from core.config import settings
# from indexing.embedder import EMBED_DIM
# from core.logger import get_logger
import time

logger = get_logger(__name__)

COLLECTION_NAME = "colep_steps"
UPSERT_BATCH_SIZE = 50

def get_qdrant_client() -> QdrantClient:
    return QdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY.get_secret_value(),
        timeout=60,
    )

def ensure_collection(client: QdrantClient):

    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
        )
        logger.info("Collection Created")
    else:
        logger.info("Collection Already Exists")


def upsert_chunks(client: QdrantClient, chunks: list[dict], vectors: list[list[float]], max_retries: int = 3):
  

    points = [
        models.PointStruct(id=c["id"], vector=v, payload={**c["payload"], "text": c["text"]})
        for c, v in zip(chunks, vectors)
    ]
    failed_batches = []
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i:i + UPSERT_BATCH_SIZE]
        for attempt in range(max_retries):
            try:
                client.upsert(collection_name=COLLECTION_NAME, points=batch)
                break
            except Exception as e:
                logger.warning(f"Batch {i // UPSERT_BATCH_SIZE} attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    failed_batches.append(i // UPSERT_BATCH_SIZE)
                else:
                    time.sleep(2 ** attempt)
    if failed_batches:
        logger.error(f"Failed batches: {failed_batches} — {len(failed_batches) * UPSERT_BATCH_SIZE} points not indexed")
    return failed_batches

# def upsert_chunks(client: QdrantClient, chunks: list[dict], vectors: list[list[float]]):
#     points = [
#         models.PointStruct(id=c["id"], vector=v, payload={**c["payload"], "text": c["text"]})
#         for c, v in zip(chunks, vectors)
#     ]
#     for i in range(0, len(points), UPSERT_BATCH_SIZE):
#         batch = points[i:i + UPSERT_BATCH_SIZE]
#         client.upsert(collection_name=COLLECTION_NAME, points=batch)