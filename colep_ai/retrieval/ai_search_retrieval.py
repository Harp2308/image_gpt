"""
ai_search_retrieval.py

Hybrid retrieval (BM25 + vector, Azure RRF) over the colep-page-based-chunks index.
Language-gated: PT / EN only. 'Other' → early rejection.
Line number → OData pre-filter on line_number collection field.
"""

import json
import re
import time
from dataclasses import dataclass
import asyncio
from azure.core.exceptions import HttpResponseError
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from lingua import Language, LanguageDetectorBuilder
from openai import AsyncAzureOpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from colep_ai.core.logger import get_logger
from colep_ai.database.azure_search_client import get_search_client
from colep_ai.indexing.embedder import  get_openai_client
from colep_ai.core.config import settings
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOP_K = 5
_SCORE_THRESHOLD = 0.0           # DEBUG: disabled — tune after confirming recall
_LANG_CONFIDENCE_THRESHOLD = 0.65
_LINE_RE = re.compile(r"\b(?:linha|line|linea|l)(?:\s+(?:number|no\.?|num\.?))?\s*(\d+)\b", re.IGNORECASE)

_LANGUAGE_DETECTOR = (
    LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.PORTUGUESE).build()
)

# Map language → (text_field_for_BM25, vector_field_for_ANN)
_LANG_FIELD_MAP = {
    "pt": ("text_pt", "vector_text_pt"),
    "en": ("text_en", "vector_text_en"),
}

_RETRYABLE = (HttpResponseError, ConnectionError, TimeoutError)


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetrievalResponse:
    results: list[dict]
    language: str           # 'pt' | 'en'
    line_filter: int | None


@dataclass(frozen=True)
class RetrievalRejected:
    reason: str             # returned to caller to surface to user


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def _detect_language(query: str) -> str:
    """Returns 'pt', 'en', or 'other'."""
    confidence_values = _LANGUAGE_DETECTOR.compute_language_confidence_values(query)
    if not confidence_values:
        return "other"
    top = confidence_values[0]
    if top.value < _LANG_CONFIDENCE_THRESHOLD:
        return "other"
    lang = "pt" if top.language == Language.PORTUGUESE else "en"
    logger.info(f"Language detected: '{lang}' ({top.value:.2f}) | query='{query[:60]}'")
    return lang


# ---------------------------------------------------------------------------
# Line number extraction
# ---------------------------------------------------------------------------

def _extract_line_number(query: str) -> int | None:
    match = _LINE_RE.search(query)
    if match:
        val = int(match.group(1))
        logger.info(f"Line number extracted: {val}")
        return val
    return None


def _build_odata_filter(line_number: int | None) -> str | None:
    if line_number is None:
        return None
    return f"line_number/any(l: l eq {line_number})"


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    reraise=True,
)
async def _embed(client: AsyncAzureOpenAI, text: str) -> list[float]:
    resp = await client.embeddings.create(model=settings.EMBED_MODEL, input=[text])
    return resp.data[0].embedding


# ---------------------------------------------------------------------------
# Azure hybrid search — stays sync (no async Azure Search SDK)
# Offloaded to executor in retrieve() below.
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)
def _hybrid_search(
    search_client: SearchClient,
    query_text: str,
    query_vector: list[float],
    text_field: str,
    vector_field: str,
    odata_filter: str | None,
    top_k: int,
) -> list[dict]:
    vector_query = VectorizedQuery(
        vector=query_vector,
        fields=vector_field,
        k_nearest_neighbors=top_k,
        exhaustive=False,
    )

    results = search_client.search(
        search_text=query_text,
        search_fields=[text_field],
        vector_queries=[vector_query],
        filter=odata_filter,
        top=top_k,
        select="*",                 # return full document; generation layer filters
    )

    hits = []
    for r in results:
        score = r.get("@search.score", 0.0)
        if score < _SCORE_THRESHOLD:
            logger.debug(f"Dropping result below threshold: score={score:.6f}")
            continue

        payload = {
            "id": r.get("id"),
            "source_file": r.get("source_file"),
            "document_code": r.get("document_code"),
            "document_title": r.get("document_title"),
            "page_number": r.get("page_number"),
            "line_number": r.get("line_number", []),
            "page_image_ids": _safe_json_loads(r.get("page_image_ids", "[]")),
            "text_pt": r.get("text_pt", ""),
            "text_en": r.get("text_en", ""),
            "image_desc": r.get("image_desc", ""),
            "entries": _safe_json_loads(r.get("entries", "[]")),
            "legend": _safe_json_loads(r.get("legend", "[]")),
            "flowchart": _safe_json_loads(r.get("flowchart", "{}")),
            "score": round(score, 6),
        }
        hits.append(payload)

    logger.info(
        f"Azure search returned {len(hits)} results above threshold "
        f"| field=({text_field},{vector_field}) | filter={odata_filter}"
    )
    return hits


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_json_loads(value: str | list | dict | None) -> list | dict:
    if isinstance(value, (list, dict)):
        return value
    if not value:
        # return empty container matching expected type
        if value == "{}":
            return {}
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Failed to deserialize JSON field value: {value!r}")
        return {}


# ---------------------------------------------------------------------------
# Public entrypoint — async
# ---------------------------------------------------------------------------

async def retrieve(
    query: str,
    openai_client: AsyncAzureOpenAI | None = None,
    search_client: SearchClient | None = None,
    top_k: int = _TOP_K,
) -> RetrievalResponse | RetrievalRejected:
    """
    Returns RetrievalResponse on success.
    Returns RetrievalRejected if language is unsupported or no results pass threshold.
     _embed is async — awaited directly.
    _hybrid_search is sync (no async Azure Search SDK) — offloaded to the
    default executor so it doesn't block the event loop.
    """
    if openai_client is None:
        openai_client = get_openai_client()
    if search_client is None:
        search_client = get_search_client()

    # Step 1 — language gate
    language = _detect_language(query)
    if language == "other":
        logger.warning(f"Unsupported language detected for query: '{query[:60]}'")
        return RetrievalRejected(reason="Please query in Portuguese or English.")

    # Step 2 — line number filter
    line_number = _extract_line_number(query)
    odata_filter = _build_odata_filter(line_number)

    # Step 3 — embed (async, awaited directly)
    embed_start = time.time()
    query_vector = await _embed(openai_client, query)
    logger.info(f"Embedding generation took {time.time() - embed_start:.3f}s")
    
    # Step 4 — hybrid search (sync SDK, offloaded to executor)
    search_start = time.time()
    text_field, vector_field = _LANG_FIELD_MAP[language]
    loop = asyncio.get_running_loop()

    results = await loop.run_in_executor(
        None,
        lambda: _hybrid_search(
            search_client=search_client,
            query_text=query,
            query_vector=query_vector,
            text_field=text_field,
            vector_field=vector_field,
            odata_filter=odata_filter,
            top_k=top_k,
        ),
    )
    logger.info(f"Retrieval took {time.time() - search_start:.3f}s")

    # Step 5 — no results
    if not results:
        logger.warning(f"No results above threshold for query: '{query[:60]}'")
        return RetrievalRejected(reason="No relevant results found for your query.")

    logger.info(
        f"Retrieval complete | language={language} | line_filter={line_number} "
        f"| results={len(results)} | top_score={results[0]['score']}"
    )

    return RetrievalResponse(
        results=results,
        language=language,
        line_filter=line_number,
    )