"""
reranker.py

Thin async wrapper around Cohere Rerank API.
Slots in between Azure AI Search retrieval and Claude generation.

Model: rerank-multilingual-v3.0  — handles PT/EN natively.
"""

import asyncio
from functools import lru_cache

import cohere

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

_RERANK_MODEL = "rerank-multilingual-v3.0"
_RERANK_TOP_N = 10


@lru_cache(maxsize=1)
def _get_cohere_client() -> cohere.Client:
    return cohere.Client(api_key=settings.COHERE_API_KEY.get_secret_value())


async def rerank(
    query: str,
    results: list[dict],
    top_n: int = _RERANK_TOP_N,
) -> list[dict]:
    """
    Takes Azure Search hits, returns top_n reranked by Cohere.

    Each result dict must have 'text_pt' and 'text_en' (from retrieval payload).
    We concatenate document_title + text for the reranker document string so
    title context is explicit — not diluted into an embedding.
    """
    if not results:
        return results

    documents = [
        f"{r.get('document_title', '')} {r.get('text_pt') or r.get('text_en', '')}".strip()
        for r in results
    ]

    client = _get_cohere_client()
    loop = asyncio.get_running_loop()

    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.rerank(
                model=_RERANK_MODEL,
                query=query,
                documents=documents,
                top_n=top_n,
                return_documents=False,
            ),
        )
    except Exception as exc:
        # Reranker failure must not kill the request — fall back to original order
        logger.warning(f"Cohere rerank failed, falling back to original order | error={exc}")
        return results[:top_n]

    reranked = []
    for hit in response.results:
        result = results[hit.index]
        result["rerank_score"] = round(hit.relevance_score, 6)
        reranked.append(result)

    logger.info(
        f"Reranked {len(results)} → {len(reranked)} | "
        f"top_score={reranked[0]['rerank_score'] if reranked else 'n/a'}"
    )
    return reranked
