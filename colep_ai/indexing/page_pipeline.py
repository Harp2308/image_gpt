from pathlib import Path

from colep_ai.core.logger import get_logger
from colep_ai.indexing.embedder import get_openai_client, embed_texts
from colep_ai.indexing.page_chunker import chunk_all_pages_full
from colep_ai.database.qdrant_page_client import get_qdrant_client, ensure_page_collection, upsert_page_chunks

logger = get_logger("page_indexing_pipeline")


def run_page_indexing(source_file: str, openai_client=None) -> None:
    if openai_client is None:
        openai_client = get_openai_client()

    results_dir = Path(source_file)
    chunks = chunk_all_pages_full(results_dir)

    if not chunks:
        logger.warning(f"No page chunks found for {source_file}")
        return

    logger.info(f"Chunking done: {len(chunks)} page chunks")

    texts_pt = [c["text_pt"] for c in chunks]
    texts_en = [c["text_en"] for c in chunks]
    texts_img = [c["image_desc"] for c in chunks]

    logger.info("Embedding text_pt ...")
    vectors_pt = embed_texts(openai_client, texts_pt)

    logger.info("Embedding text_en ...")
    vectors_en = embed_texts(openai_client, texts_en)

    logger.info("Embedding image_desc ...")
    vectors_img = embed_texts(openai_client, texts_img)

    logger.info("All embeddings done")

    qdrant = get_qdrant_client()
    ensure_page_collection(qdrant)

    failed = upsert_page_chunks(qdrant, chunks, vectors_pt, vectors_en, vectors_img)

    if failed:
        logger.error(f"Indexing completed with {len(failed)} failed batches")
    else:
        logger.info(f"Indexing complete: {len(chunks)} pages indexed for {source_file}")