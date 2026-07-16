import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from lingua import Language, LanguageDetectorBuilder
from openai import AzureOpenAI
from qdrant_client import QdrantClient, models

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from colep_ai.core.logger import get_logger
from colep_ai.indexing.embedder import EMBED_MODEL
from colep_ai.database.qdrant_page_client import PAGE_COLLECTION_NAME

logger = get_logger(__name__)

_LANGUAGE_DETECTOR = (
    LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.PORTUGUESE).build()
)
_LANG_CONFIDENCE_THRESHOLD = 0.65
_QDRANT_TIMEOUT_SECONDS = 5
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qdrant-search")

# line number: "linha 3", "line 3", "L3", "linea 3"
_LINE_RE = re.compile(r"\b(?:linha|line|linea|l)\s*(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RetrievalResponse:
    results: list[dict]
    language: str  # 'portuguese' | 'english' | 'other'


def detect_language(query: str) -> str:
    confidence_values = _LANGUAGE_DETECTOR.compute_language_confidence_values(query)
    if not confidence_values:
        return "other"
    top = confidence_values[0]
    if top.value < _LANG_CONFIDENCE_THRESHOLD:
        return "other"
    detected = "portuguese" if top.language == Language.PORTUGUESE else "english"
    logger.info(f"Detected language: '{detected}' ({top.value:.2f}) for query: '{query[:60]}'")
    return detected


def _extract_line_number(query: str) -> int | None:
    match = _LINE_RE.search(query)
    if match:
        val = int(match.group(1))
        logger.info(f"Extracted line_number={val} from query")
        return val
    return None


def _build_line_filter(line_number: int | None) -> models.Filter | None:
    if line_number is None:
        return None
    return models.Filter(
        must=[
            models.FieldCondition(
                key="line_number",
                match=models.MatchValue(value=line_number),
            )
        ]
    )


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
    query_filter: models.Filter | None,
) -> list[dict]:
    results = qdrant.query_points(
        collection_name=PAGE_COLLECTION_NAME,
        query=vector,
        using=vector_name,
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
        with_vectors=False,
        timeout=_QDRANT_TIMEOUT_SECONDS,
    )
    return [{"payload": r.payload, "score": r.score, "source": vector_name} for r in results.points]


def _search_bm25(
    qdrant: QdrantClient,
    query_text: str,
    field: str,
    top_k: int,
    query_filter: models.Filter | None,
) -> list[dict]:
    """Full-text BM25 search on a payload field. Returns empty list on any failure."""
    if not query_text or not query_text.strip():
        return []
    try:
        text_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key=field,
                    match=models.MatchText(text=query_text.strip()),
                )
            ]
        )
        # merge with line filter if present
        combined_filter = (
            models.Filter(must=[text_filter, query_filter])
            if query_filter
            else text_filter
        )
        results = qdrant.scroll(
            collection_name=PAGE_COLLECTION_NAME,
            scroll_filter=combined_filter,
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )
        return [{"payload": r.payload, "score": 1.0, "source": f"bm25_{field}"} for r in results[0]]
    except Exception as e:
        logger.warning(f"BM25 search on '{field}' failed, skipping: {e}")
        return []


def _run_all_searches_parallel(
    qdrant: QdrantClient,
    query_vector: list[float],
    query_text: str,
    top_k: int,
    query_filter: models.Filter | None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """
    4 searches in parallel:
    - semantic text_pt
    - semantic text_en
    - BM25 text_pt
    - BM25 text_en
    """
    futures = {
        "sem_pt": _EXECUTOR.submit(_search_vector, qdrant, query_vector, "text_pt", top_k, query_filter),
        "sem_en": _EXECUTOR.submit(_search_vector, qdrant, query_vector, "text_en", top_k, query_filter),
        "bm25_pt": _EXECUTOR.submit(_search_bm25, qdrant, query_text, "text_pt", top_k, query_filter),
        "bm25_en": _EXECUTOR.submit(_search_bm25, qdrant, query_text, "text_en", top_k, query_filter),
    }

    results = {}
    for name, future in futures.items():
        try:
            results[name] = future.result(timeout=_QDRANT_TIMEOUT_SECONDS + 2)
        except FutureTimeoutError:
            logger.error(f"Search '{name}' timed out — returning empty")
            results[name] = []
        except Exception as e:
            logger.error(f"Search '{name}' failed: {e} — returning empty")
            results[name] = []

    return results["sem_pt"], results["sem_en"], results["bm25_pt"], results["bm25_en"]


RRF_K = 60


def _fuse_results(
    ranked_lists: list[list[dict]],
    top_k: int,
) -> list[dict]:
    """RRF over N ranked lists. BM25 lists have score=1.0 so rank matters, not score."""
    scores: dict[tuple, dict] = {}

    for ranked in ranked_lists:
        for rank, r in enumerate(ranked):
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
    language = detect_language(query)

    line_number = _extract_line_number(query)
    query_filter = _build_line_filter(line_number)

    if line_number is not None:
        logger.info(f"Applying line_number filter: {line_number}")

    query_vector = _embed_single(openai_client, query)

    sem_pt, sem_en, bm25_pt, bm25_en = _run_all_searches_parallel(
        qdrant_client, query_vector, query, top_k, query_filter
    )

    logger.info(
        f"Hits — sem_pt:{len(sem_pt)} sem_en:{len(sem_en)} "
        f"bm25_pt:{len(bm25_pt)} bm25_en:{len(bm25_en)}"
    )

    fused = _fuse_results([sem_pt, sem_en, bm25_pt, bm25_en], top_k)

    logger.info(f"Returning {len(fused)} fused results | language={language} | line_filter={line_number}")
    return RetrievalResponse(results=fused, language=language)