
# from colep_ai.core.config import settings
# from colep_ai.indexing.chunker import chunk_all_pages
# from colep_ai.indexing.embedder import  embed_texts
# from colep_ai.database.qdrant_db_client import get_qdrant_client, ensure_collection, upsert_chunks
# from colep_ai.core.logger import get_logger

from pathlib import Path
from core.config import settings
from indexing.chunker import chunk_all_pages
from indexing.embedder import get_openai_client, embed_texts
from database.qdrant_db_client import get_qdrant_client, ensure_collection, upsert_chunks
from core.logger import get_logger

logger = get_logger("Indexing_pipeline")


def run_indexing(source_file: str,openai_client):
    
    results_dir =Path(source_file)
    chunks = chunk_all_pages(results_dir)
    if not chunks:
        logger.warning(f"No chunks found for {source_file}")
        return
    logger.info(" Chunking Done ")

    
    texts = [c["text"] for c in chunks]
    vectors = embed_texts(openai_client, texts)
    logger.info(" Emb Done ")

    qdrant = get_qdrant_client()
    ensure_collection(qdrant)
    upsert_chunks(qdrant, chunks, vectors)

    logger.info(f"Indexed {len(chunks)} chunks for {source_file}")
    logger.info(" Process Completed ")


# if __name__=="__main__":
#     p=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\outputs\e1\results"
#     run_indexing(p)