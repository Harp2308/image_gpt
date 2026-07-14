import time

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from openai import OpenAI
from pydantic import BaseModel
from qdrant_client import QdrantClient

from colep_ai.core.logger import get_logger
from colep_ai.retrieval.page_retrieval import retrieve
from colep_ai.generation.answer_builder import generate_from_retrieval
from colep_ai.api.dependencies import get_openai, get_claude, get_qdrant

logger = get_logger(__name__)
router = APIRouter(tags=["Chat"])


def _log_stage(stage: str, start: float) -> None:
    logger.info(f"Stage {stage} | elapsed={time.monotonic() - start:.2f}s")


class QueryRequest(BaseModel):
    query: str
    top_k: int = 5

class Citation(BaseModel):
    marker: str
    image_url: str | None
    image_description: str

class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    language: str


def _resolve_image_url(image_ref: str | None, source_file: str, page_number: int) -> str | None:
    if not image_ref:
        return None
    if image_ref.endswith(".png") and "_" in image_ref:
        return f"/outputs/{source_file}/combined/page_{page_number}/{image_ref}"
    return f"/outputs/{source_file}/crops/page_{page_number}/crops/{image_ref}.png"


@router.post("/query", response_model=QueryResponse)
def query(
    req: QueryRequest,
    openai_client: OpenAI = Depends(get_openai),
    claude_client: anthropic.Anthropic = Depends(get_claude),
    qdrant_client: QdrantClient = Depends(get_qdrant),
):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    request_start = time.monotonic()

    stage_start = time.monotonic()
    retrieval_response = retrieve(
        query=req.query,
        openai_client=openai_client,
        qdrant_client=qdrant_client,
        top_k=req.top_k,
    )
    _log_stage("retrieval", stage_start)

    stage_start = time.monotonic()
    generation_output = generate_from_retrieval(
        claude_client=claude_client,
        query=req.query,
        retrieval_response=retrieval_response,
    )
    _log_stage("generation", stage_start)

    stage_start = time.monotonic()
    marker_meta: dict[str, tuple[str, int]] = {}
    for result in retrieval_response.results:
        payload = result["payload"]
        sf = payload.get("source_file", "")
        pn = payload.get("page_number", 0)
        for idx, _ in enumerate(payload.get("entries", []), start=1):
            marker_meta[f"[page {pn} | entry {idx}]"] = (sf, pn)

    citations = [
        Citation(
            marker=c["marker"],
            image_url=_resolve_image_url(
                c["image_ref"],
                *marker_meta.get(c["marker"], ("", 0)),
            ),
            image_description=c.get("image_description", ""),
        )
        for c in generation_output["citations"]
    ]
    _log_stage("citation_resolution", stage_start)

    _log_stage("total_request", request_start)

    return QueryResponse(
        answer=generation_output["answer"],
        citations=citations,
        language=generation_output["language"],
    )