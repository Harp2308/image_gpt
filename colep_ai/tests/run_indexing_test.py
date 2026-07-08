import logging
from pathlib import Path

from indexing.chunker import chunk_all_pages
from indexing.embedder import get_openai_client, embed_texts
from database.qdrant_db_client import get_qdrant_client, ensure_collection, upsert_chunks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_indexing_from_folder(results_dir: str, source_file: str):
    results_dir = Path(results_dir)
    chunks = chunk_all_pages(results_dir, source_file)
    if not chunks:
        logger.warning("No chunks found in %s for %s", results_dir, source_file)
        return

    openai_client = get_openai_client()
    texts = [c["text"] for c in chunks]
    vectors = embed_texts(openai_client, texts)

    qdrant = get_qdrant_client()
    ensure_collection(qdrant)
    upsert_chunks(qdrant, chunks, vectors)

    logger.info("Indexed %d chunks for %s", len(chunks), source_file)

if __name__ == "__main__":
    run_indexing_from_folder(
        results_dir=r"path\to\your\json\folder",
        source_file="e5",  # must match the prefix in your json filenames: e5_page_1_result.json etc.
    )