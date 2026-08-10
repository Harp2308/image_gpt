"""
azure_search_client.py
Drop-in replacement for qdrant_page_client.py.

Mirrors the same public API:
    get_search_client()         -> SearchClient
    ensure_page_index()         -> None
    upsert_page_chunks()        -> list[int]  (failed batch indexes)

Index name mirrors the Qdrant collection name.
3 vector fields: text_pt, text_en, image_desc  (each 3072-dim, cosine).
"""

import json
import time
from typing import Any

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    CorsOptions,
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

INDEX_NAME = settings.INDEX_NAME
UPSERT_BATCH_SIZE = 50  # Azure max per upload call is 1000, but keep aligned with Qdrant


# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------

def _credential() -> AzureKeyCredential:
    return AzureKeyCredential(settings.AZURE_SEARCH_API_KEY.get_secret_value())


def get_index_client() -> SearchIndexClient:
    return SearchIndexClient(
        endpoint=settings.AZURE_SEARCH_ENDPOINT,
        credential=_credential(),
    )


def get_search_client() -> SearchClient:
    logger.info(
                f"Search client initiated |index ={INDEX_NAME} | "  
              )
    return SearchClient(
        endpoint=settings.AZURE_SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=_credential(),
    )
    


# ---------------------------------------------------------------------------
# Index schema
# ---------------------------------------------------------------------------

def _build_index() -> SearchIndex:
    # One HNSW config, reused for all 3 vector fields.
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="hnsw-config",
                parameters=HnswParameters(
                    m=16,
                    ef_construction=400,
                    ef_search=500,
                    metric="cosine",
                ),
            )
        ],
        profiles=[
            VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw-config"),
        ],
    )

    fields = [
        # --- key ---
        SimpleField(
            name="id",
            type=SearchFieldDataType.String,
            key=True,
            filterable=True,
        ),

        # --- keyword / filter fields ---
        SimpleField(
            name="folder_name",
            type=SearchFieldDataType.String,
            filterable=True,   # ← allows WHERE folder_name eq 'Line5'
            retrievable=True,  # ← returns it in search results
        ),
        SimpleField(
            name="source_file",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=False,
        ),
        SimpleField(
            name="sheet_name",
            type=SearchFieldDataType.String,
            filterable=True,
            retrievable=True,
        ),
        SimpleField(
            name="periodicity",
            type=SearchFieldDataType.String,
            filterable=True,
            retrievable=True,
        ),
        SimpleField(
            name="document_code",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=False,
        ),
        SimpleField(
            name="document_title",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=False,
        ),
        SimpleField(
            name="page_number",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="line_number",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Int32),
            filterable=True,
            collection=True, 
        ),
        SimpleField(
            name="page_image_ids",
            type=SearchFieldDataType.String,   # JSON-serialised list
            filterable=False,
        ),

        # --- full-text searchable ---
        SearchableField(
            name="text_pt",
            type=SearchFieldDataType.String,
            analyzer_name="pt-Pt.microsoft",
        ),
        SearchableField(
            name="text_en",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft",
        ),
        SearchableField(
            name="image_desc",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft",
        ),

        # --- stored blobs (not searched, returned in results) ---
        SimpleField(
            name="entries",
            type=SearchFieldDataType.String,   # JSON-serialised list
            filterable=False,
        ),
        SimpleField(
            name="legend",
            type=SearchFieldDataType.String,   # JSON-serialised list
            filterable=False,
        ),
        SimpleField(
            name="flowchart",
            type=SearchFieldDataType.String,   # JSON-serialised dict
            filterable=False,
        ),
         SimpleField(
            name="map",
            type=SearchFieldDataType.String,   # JSON-serialised dict
            filterable=False,
        ),

        # --- vector fields ---
        SearchField(
            name="vector_text_pt",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=settings.EMBED_DIM,
            vector_search_profile_name="hnsw-profile",
        ),
        SearchField(
            name="vector_text_en",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=settings.EMBED_DIM,
            vector_search_profile_name="hnsw-profile",
        ),
        SearchField(
            name="vector_image_desc",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=settings.EMBED_DIM,
            vector_search_profile_name="hnsw-profile",
        ),
    ]

    return SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
        cors_options=CorsOptions(allowed_origins=["*"]),
    )


# ---------------------------------------------------------------------------
# Ensure index (idempotent)
# ---------------------------------------------------------------------------

from azure.core.exceptions import ResourceExistsError

def ensure_page_index(client: SearchIndexClient | None = None) -> None:
    if client is None:
        client = get_index_client()
    try:
        client.create_or_update_index(_build_index())
        logger.info(f"Index ensured: {INDEX_NAME}")
    except ResourceExistsError:
        logger.info(f"Index already exists or concurrent creation in progress — skipping: {INDEX_NAME}")

# ---------------------------------------------------------------------------
# Upsert — same signature as qdrant_page_client.upsert_page_chunks()
# ---------------------------------------------------------------------------

def upsert_page_chunks(
    client: SearchClient,
    chunks: list[dict],
    vectors_pt: list[list[float]],
    vectors_en: list[list[float]],
    vectors_img: list[list[float]],
    max_retries: int = 3,
) -> list[int]:
    """
    Returns list of failed batch indexes (empty = all succeeded).
    chunks[i]["payload"] must contain the standard payload keys.
    chunks[i]["id"] is the document key (UUID string).
    """
    documents = []
    for chunk, v_pt, v_en, v_img in zip(chunks, vectors_pt, vectors_en, vectors_img):
        payload: dict[str, Any] = chunk["payload"]
        documents.append(
            {
                "id": chunk["id"],
                # payload scalars
                "folder_name": payload.get("folder_name", ""),
                "source_file": payload.get("source_file", ""),
                "sheet_name": payload.get("sheet_name", ""),
                "periodicity": payload.get("periodicity", ""),
                "document_code": payload.get("document_code", ""),
                "document_title": payload.get("document_title", ""),
                "page_number": payload.get("page_number", 0),
                "line_number": payload.get("line_number") or [],
                "page_image_ids": json.dumps(payload.get("page_image_ids", [])),
                # full-text
                "text_pt": chunk.get("text_pt", ""),
                "text_en": chunk.get("text_en", ""),
                "image_desc": chunk.get("image_desc", ""),
                # stored blobs — serialise to JSON string
                "entries": json.dumps(payload.get("entries", []), ensure_ascii=False),
                "legend": json.dumps(payload.get("legend", []), ensure_ascii=False),
                "flowchart": json.dumps(payload.get("flowchart", {}), ensure_ascii=False),
                "map": json.dumps(payload.get("map", {}), ensure_ascii=False),
                # vectors
                "vector_text_pt": v_pt,
                "vector_text_en": v_en,
                "vector_image_desc": v_img,
            }
        )

    failed_batches: list[int] = []

    for i in range(0, len(documents), UPSERT_BATCH_SIZE):
        batch = documents[i: i + UPSERT_BATCH_SIZE]
        batch_index = i // UPSERT_BATCH_SIZE

        for attempt in range(max_retries):
            try:
                result = client.merge_or_upload_documents(documents=batch)
                failed_in_batch = [r for r in result if not r.succeeded]
                if failed_in_batch:
                    logger.warning(
                        f"Batch {batch_index}: {len(failed_in_batch)} docs failed — "
                        + ", ".join(f"{r.key}: {r.error_message}" for r in failed_in_batch)
                    )
                break
            except HttpResponseError as e:
                logger.warning(f"Batch {batch_index} attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    failed_batches.append(batch_index)
                else:
                    time.sleep(2**attempt)

    if failed_batches:
        logger.error(
            f"Failed batches: {failed_batches} — "
            f"~{len(failed_batches) * UPSERT_BATCH_SIZE} docs not indexed"
        )

    return failed_batches

# -------------------------------------------------------------------------------------------
#  delete by folder name 
# -------------------------------------------------------------------------------------------

def delete_by_folder_name(client: SearchClient, folder_name: str) -> int:
    total_deleted = 0

    while True:
        results = list(client.search(
            search_text="*",
            filter=f"folder_name eq '{folder_name}'",
            select=["id"],
            top=1000,
        ))
        if not results:
            break

        batch = [{"id": r["id"]} for r in results]
        client.delete_documents(documents=batch)
        total_deleted += len(batch)

    logger.info(f"Deleted {total_deleted} chunks for folder_name='{folder_name}'")
    return total_deleted


def delete_by_source_file(client: SearchClient, source_file: str) -> int:
    total_deleted = 0

    while True:
        results = list(client.search(
            search_text="*",
            filter=f"source_file eq '{source_file}'",
            select=["id"],
            top=1000,
        ))
        if not results:
            break

        batch = [{"id": r["id"]} for r in results]
        client.delete_documents(documents=batch)
        total_deleted += len(batch)

    logger.info(f"Deleted {total_deleted} chunks for source_file='{source_file}'")
    return total_deleted

