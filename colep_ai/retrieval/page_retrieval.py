from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from lingua import Language, LanguageDetectorBuilder
from openai import AzureOpenAI
from qdrant_client import QdrantClient
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from colep_ai.core.logger import get_logger
from colep_ai.indexing.embedder import EMBED_MODEL
from colep_ai.database.qdrant_page_client import PAGE_COLLECTION_NAME

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# Language detection: local, no network call, no per-query LLM cost/latency.
# Restricted to PT/EN since that's all the routing logic branches on.
# Built once at import time (Lingua's detector build cost is non-trivial).
# --------------------------------------------------------------------------
_LANGUAGE_DETECTOR = (
    LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.PORTUGUESE)
    .build()
)

_LANG_CONFIDENCE_THRESHOLD = 0.65

_QDRANT_TIMEOUT_SECONDS = 5
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qdrant-search")


@dataclass(frozen=True)
class RetrievalResponse:
    """
    Wraps fused retrieval results with the detected query language so
    downstream context-formatting and answer-generation stay language-consistent
    without re-detecting on the answer side.
    """
    results: list[dict]
    language: str  # 'portuguese' | 'english' | 'other'


def detect_language(query: str) -> str:
    """Returns 'portuguese', 'english', or 'other'."""
    confidence_values = _LANGUAGE_DETECTOR.compute_language_confidence_values(query)

    if not confidence_values:
        logger.info(f"Language detection produced no result for query: '{query[:60]}'")
        return "other"

    top = confidence_values[0]

    if top.value < _LANG_CONFIDENCE_THRESHOLD:
        logger.info(
            f"Low-confidence language detection ({top.value:.2f}) for "
            f"query: '{query[:60]}' -> defaulting to 'other'"
        )
        return "other"

    detected = "portuguese" if top.language == Language.PORTUGUESE else "english"
    logger.info(f"Detected language: '{detected}' ({top.value:.2f}) for query: '{query[:60]}'")
    return detected


# --------------------------------------------------------------------------
# Retry policy: transient failures only.
# --------------------------------------------------------------------------
_RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    reraise=True,
)
def _embed_single(client: AzureOpenAI, text: str) -> list[float]:
    resp = client.embeddings.create(model=EMBED_MODEL, input=[text])
    return resp.data[0].embedding


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    reraise=True,
)
def _search_vector(
    qdrant: QdrantClient,
    vector: list[float],
    vector_name: str,
    top_k: int,
) -> list[dict]:
    results = qdrant.query_points(
        collection_name=PAGE_COLLECTION_NAME,
        query=vector,
        using=vector_name,
        limit=top_k,
        with_payload=True,
        with_vectors=False,
        timeout=_QDRANT_TIMEOUT_SECONDS,
    )
    return [{"payload": r.payload, "score": r.score} for r in results.points]


def _search_pt_and_en_parallel(
    qdrant: QdrantClient,
    query_vector: list[float],
    top_k: int,
) -> tuple[list[dict], list[dict]]:
    """
    Always searches both text_pt and text_en concurrently regardless of query language.
    Cross-lingual embeddings (text-embedding-3-large) handle the semantic bridge —
    a Portuguese query against text_en vectors and vice versa still finds relevant pages.
    Searching both ensures neither language-indexed content is missed.
    """
    pt_future = _EXECUTOR.submit(
        _search_vector, qdrant, query_vector, "text_pt", top_k
    )
    en_future = _EXECUTOR.submit(
        _search_vector, qdrant, query_vector, "text_en", top_k
    )

    try:
        pt_results = pt_future.result(timeout=_QDRANT_TIMEOUT_SECONDS + 2)
    except FutureTimeoutError:
        logger.error("Vector search 'text_pt' timed out")
        raise

    try:
        en_results = en_future.result(timeout=_QDRANT_TIMEOUT_SECONDS + 2)
    except FutureTimeoutError:
        logger.error("Vector search 'text_en' timed out")
        raise

    return pt_results, en_results


RRF_K = 60  # standard constant


def _fuse_results(
    pt_results: list[dict],
    en_results: list[dict],
    top_k: int,
) -> list[dict]:
    """
    RRF fusion over text_pt and text_en ranked lists.
    Pages appearing in both lists get a higher combined score.
    """
    scores: dict[tuple, dict] = {}

    for rank, r in enumerate(pt_results):
        key = (r["payload"]["source_file"], r["payload"]["page_number"])
        scores.setdefault(key, {"payload": r["payload"], "rrf_score": 0.0})
        scores[key]["rrf_score"] += 1 / (RRF_K + rank + 1)

    for rank, r in enumerate(en_results):
        key = (r["payload"]["source_file"], r["payload"]["page_number"])
        scores.setdefault(key, {"payload": r["payload"], "rrf_score": 0.0})
        scores[key]["rrf_score"] += 1 / (RRF_K + rank + 1)

    fused = [
        {"payload": v["payload"], "score": round(v["rrf_score"], 6)}
        for v in scores.values()
    ]
    fused.sort(key=lambda x: x["score"], reverse=True)
    return fused[:top_k]


def retrieve(
    query: str,
    openai_client: AzureOpenAI,
    qdrant_client: QdrantClient,
    top_k: int = 7,
) -> RetrievalResponse:
    """
    Returns RetrievalResponse(results, language).

    Searches text_pt and text_en vectors in parallel, always — no language routing.
    image_desc vector is intentionally excluded; re-enable when visual queries are supported.

    results is a list of dicts: {"payload": {...}, "score": float}
    """
    language = detect_language(query)

    logger.info(f"Query language='{language}' | Searching text_pt + text_en | top_k={top_k}")

    query_vector = _embed_single(openai_client, query)

    pt_results, en_results = _search_pt_and_en_parallel(
        qdrant_client, query_vector, top_k
    )

    logger.info(f"text_pt hits: {len(pt_results)} | text_en hits: {len(en_results)}")

    fused = _fuse_results(pt_results, en_results, top_k)

    logger.info(f"Returning {len(fused)} fused results")
    return RetrievalResponse(results=fused, language=language)


# v1 with img_simi
# from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
# from dataclasses import dataclass

# from lingua import Language, LanguageDetectorBuilder
# from openai import AzureOpenAI
# from qdrant_client import QdrantClient
# from tenacity import (
#     retry,
#     stop_after_attempt,
#     wait_exponential,
#     retry_if_exception_type,
# )

# from colep_ai.core.logger import get_logger
# from colep_ai.indexing.embedder import EMBED_MODEL
# from colep_ai.database.qdrant_page_client import PAGE_COLLECTION_NAME
# logger = get_logger(__name__)

# # --------------------------------------------------------------------------
# # Language detection: local, no network call, no per-query LLM cost/latency.
# # Restricted to PT/EN since that's all the routing logic branches on -
# # no reason to pay for a 75-language model when only two matter.
# # Built once at import time (Lingua's detector build cost is non-trivial,
# # so we do NOT rebuild it per request).
# # --------------------------------------------------------------------------
# _LANGUAGE_DETECTOR = (
#     LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.PORTUGUESE)
#     .build()
# )

# # Confidence threshold below which we fall back to "other" / default routing
# # rather than trusting a low-confidence guess. Tune against your real query logs.
# _LANG_CONFIDENCE_THRESHOLD = 0.65

# _QDRANT_TIMEOUT_SECONDS = 5
# _EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qdrant-search")


# @dataclass(frozen=True)
# class RetrievalResponse:
#     """
#     Wraps fused retrieval results with the detected query language so
#     downstream context-formatting and answer-generation stay consistent
#     with the language used for vector search - avoids re-detecting
#     language a second time and risking disagreement between the two.
#     """
#     results: list[dict]
#     language: str  # 'portuguese' | 'english' | 'other'


# def detect_language(query: str) -> str:
#     """
#     Returns 'portuguese', 'english', or 'other'.

#     Local detection - no network round trip, no LLM cost, sub-millisecond.
#     Replaces the previous GPT-4o-mini classification call.
#     """
#     confidence_values = _LANGUAGE_DETECTOR.compute_language_confidence_values(query)

#     if not confidence_values:
#         logger.info(f"Language detection produced no result for query: '{query[:60]}'")
#         return "other"

#     top = confidence_values[0]

#     if top.value < _LANG_CONFIDENCE_THRESHOLD:
#         logger.info(
#             f"Low-confidence language detection ({top.value:.2f}) for "
#             f"query: '{query[:60]}' -> defaulting to 'other'"
#         )
#         return "other"

#     detected = "portuguese" if top.language == Language.PORTUGUESE else "english"
#     logger.info(f"Detected language: '{detected}' ({top.value:.2f}) for query: '{query[:60]}'")
#     return detected


# # --------------------------------------------------------------------------
# # Retry policy: only for transient failures (network blips, momentary
# # unavailability). We do NOT retry on programming/validation errors -
# # those should fail fast and loud.
# # --------------------------------------------------------------------------
# _RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)


# @retry(
#     stop=stop_after_attempt(3),
#     wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
#     retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
#     reraise=True,
# )
# def _embed_single(client: AzureOpenAI, text: str) -> list[float]:
#     resp = client.embeddings.create(model=EMBED_MODEL, input=[text])
#     return resp.data[0].embedding


# @retry(
#     stop=stop_after_attempt(3),
#     wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
#     retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
#     reraise=True,
# )
# def _search_vector(
#     qdrant: QdrantClient,
#     vector: list[float],
#     vector_name: str,
#     top_k: int,
# ) -> list[dict]:
#     results = qdrant.query_points(
#         collection_name=PAGE_COLLECTION_NAME,
#         query=vector,
#         using=vector_name,
#         limit=top_k,
#         with_payload=True,
#         with_vectors=False,
#         timeout=_QDRANT_TIMEOUT_SECONDS,
#     )
#     return [{"payload": r.payload, "score": r.score} for r in results.points]


# def _search_both_vectors_parallel(
#     qdrant: QdrantClient,
#     query_vector: list[float],
#     primary_vector_name: str,
#     top_k: int,
# ) -> tuple[list[dict], list[dict]]:
#     """
#     Runs the primary-text search and the image-desc search concurrently.
#     They're independent reads against the same collection - no reason to
#     serialize them and double the latency.
#     """
#     primary_future = _EXECUTOR.submit(
#         _search_vector, qdrant, query_vector, primary_vector_name, top_k
#     )
#     image_future = _EXECUTOR.submit(
#         _search_vector, qdrant, query_vector, "image_desc", top_k
#     )

#     try:
#         primary_results = primary_future.result(timeout=_QDRANT_TIMEOUT_SECONDS + 2)
#     except FutureTimeoutError:
#         logger.error(f"Primary vector search ('{primary_vector_name}') timed out")
#         raise

#     try:
#         image_results = image_future.result(timeout=_QDRANT_TIMEOUT_SECONDS + 2)
#     except FutureTimeoutError:
#         logger.error("Image vector search ('image_desc') timed out")
#         raise

#     return primary_results, image_results


# # def _fuse_results(
# #     primary_results: list[dict],
# #     image_results: list[dict],
# #     primary_weight: float,
# #     image_weight: float,
# #     top_k: int,
# # ) -> list[dict]:
# #     """
# #     Weighted score fusion by page identity (source_file + page_number).
# #     """
# #     scores: dict[tuple, dict] = {}

# #     for r in primary_results:
# #         key = (r["payload"]["source_file"], r["payload"]["page_number"])
# #         scores[key] = {
# #             "payload": r["payload"],
# #             "primary_score": r["score"],
# #             "image_score": 0.0,
# #         }

# #     for r in image_results:
# #         key = (r["payload"]["source_file"], r["payload"]["page_number"])
# #         if key in scores:
# #             scores[key]["image_score"] = r["score"]
# #         else:
# #             scores[key] = {
# #                 "payload": r["payload"],
# #                 "primary_score": 0.0,
# #                 "image_score": r["score"],
# #             }

# #     fused = []
# #     for val in scores.values():
# #         combined = (
# #             primary_weight * val["primary_score"]
# #             + image_weight * val["image_score"]
# #         )
# #         fused.append({
# #             "payload": val["payload"],
# #             "score": round(combined, 6),
# #             "primary_score": round(val["primary_score"], 6),
# #             "image_score": round(val["image_score"], 6),
# #         })

# #     fused.sort(key=lambda x: x["score"], reverse=True)
# #     return fused[:top_k]

# RRF_K = 60  # standard constant

# def _fuse_results(primary_results, image_results, top_k) -> list[dict]:
#     scores: dict[tuple, dict] = {}

#     for rank, r in enumerate(primary_results):
#         key = (r["payload"]["source_file"], r["payload"]["page_number"])
#         scores.setdefault(key, {"payload": r["payload"], "rrf_score": 0.0})
#         scores[key]["rrf_score"] += 1 / (RRF_K + rank + 1)

#     for rank, r in enumerate(image_results):
#         key = (r["payload"]["source_file"], r["payload"]["page_number"])
#         scores.setdefault(key, {"payload": r["payload"], "rrf_score": 0.0})
#         scores[key]["rrf_score"] += 1 / (RRF_K + rank + 1)

#     fused = [{"payload": v["payload"], "score": round(v["rrf_score"], 6)} for v in scores.values()]
#     fused.sort(key=lambda x: x["score"], reverse=True)
#     return fused[:top_k]

# def retrieve(
#     query: str,
#     openai_client: AzureOpenAI,
#     qdrant_client: QdrantClient,
#     top_k: int = 7,
#     primary_weight: float = 0.7,
#     image_weight: float = 0.3,
# ) -> RetrievalResponse:
#     """
#     Returns a RetrievalResponse(results, language).

#     BREAKING CHANGE: previously returned list[dict] directly. Now returns
#     RetrievalResponse so callers can access the detected language without
#     re-running detection (needed for consistent context-formatting /
#     answer-language selection downstream). Existing call sites need to
#     switch from `retrieve(...)` to `retrieve(...).results`.

#     results is a list of dicts:
#         {
#             "payload": {...},
#             "score": float,
#             "primary_score": float,
#             "image_score": float,
#         }
#     """
#     language = detect_language(query)

#     if language == "portuguese":
#         primary_vector_name = "text_pt"
#     else:
#         primary_vector_name = "text_en"

#     logger.info(f"Searching vector='{primary_vector_name}' top_k={top_k}")

#     query_vector = _embed_single(openai_client, query)

#     primary_results, image_results = _search_both_vectors_parallel(
#         qdrant_client, query_vector, primary_vector_name, top_k
#     )

#     logger.info(f"Primary results: {len(primary_results)} | Image results: {len(image_results)}")

#     fused = _fuse_results(primary_results, image_results, top_k)

#     logger.info(f"Returning {len(fused)} fused results")
#     return RetrievalResponse(results=fused, language=language)