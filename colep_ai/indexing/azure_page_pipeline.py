"""
azure_page_pipeline.py
Drop-in replacement for page_pipeline.py.

Swaps Qdrant client for Azure AI Search client.
Everything else (chunking, embedding) is identical.
"""

from pathlib import Path

from colep_ai.core.logger import get_logger
from colep_ai.indexing.embedder import get_openai_client, embed_texts
from colep_ai.indexing.page_chunker import chunk_all_pages_full
from colep_ai.database.azure_search_client import (
    get_index_client,
    get_search_client,
    ensure_page_index,
    upsert_page_chunks,
)

logger = get_logger("azure_page_indexing_pipeline")


def _deduplicate_chunks(chunks: list[dict]) -> list[dict]:
    """
    Drop chunks whose every entry is a duplicate of an entry already seen.
    Dedup key per entry: (entry_text, frozenset of fields.items())
    Source JSONs on disk are never touched.
    """
    seen_entry_keys: set[tuple] = set()
    deduplicated: list[dict] = []

    for chunk in chunks:
        page_number = chunk["payload"]["page_number"]
        source_file = chunk["payload"]["source_file"]
        entries = chunk["payload"]["entries"]

        if not entries:
            deduplicated.append(chunk)
            continue

        entry_keys = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_text = entry.get("entry_text", "").strip()
            fields = entry.get("fields", {})
            key = (entry_text, frozenset(fields.items()) if isinstance(fields, dict) else frozenset())
            entry_keys.append(key)

        if not entry_keys:
            deduplicated.append(chunk)
            continue

        all_seen = all(k in seen_entry_keys for k in entry_keys)

        if all_seen:
            # find which page we first saw this content
            first_seen_page = next(
                c["payload"]["page_number"]
                for c in deduplicated
                if any(
                    (e.get("entry_text", "").strip(), frozenset(e.get("fields", {}).items()) if isinstance(e.get("fields"), dict) else frozenset())
                    in seen_entry_keys
                    for e in c["payload"]["entries"]
                    if isinstance(e, dict)
                )
            )
            logger.info(
                f"Skipping page {page_number} of {source_file} — "
                f"duplicate content already seen in page {first_seen_page}"
            )
            continue

        for k in entry_keys:
            seen_entry_keys.add(k)
        deduplicated.append(chunk)

    return deduplicated

def run_page_indexing(source_file: str, openai_client=None,folder_name: str = "") -> None:
    if openai_client is None:
        openai_client = get_openai_client()

    results_dir = Path(source_file)
    chunks = chunk_all_pages_full(results_dir)

    if not chunks:
        logger.warning(f"No page chunks found for {source_file}")
        return

    logger.info(f"Chunking done: {len(chunks)} page chunks")

    chunks = _deduplicate_chunks(chunks)
    logger.info(f"After dedup: {len(chunks)} page chunks will be indexed")

    texts_pt  = [c["text_pt"]    for c in chunks]
    texts_en  = [c["text_en"]    for c in chunks]
    texts_img = [c["image_desc"] for c in chunks]

    logger.info("Embedding text_pt ...")
    vectors_pt = embed_texts(openai_client, texts_pt)

    logger.info("Embedding text_en ...")
    vectors_en = embed_texts(openai_client, texts_en)

    logger.info("Embedding image_desc ...")
    vectors_img = embed_texts(openai_client, texts_img)

    logger.info("All embeddings done")

    # Ensure index exists (idempotent)
    ensure_page_index(get_index_client())

    search_client = get_search_client()
    failed = upsert_page_chunks(search_client, chunks, vectors_pt, vectors_en, vectors_img)

    if failed:
        logger.error(f"Indexing completed with {len(failed)} failed batches")
    else:
        logger.info(f"Indexing complete: {len(chunks)} pages indexed for {source_file}")
