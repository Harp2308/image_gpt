import time

import anthropic
from azure.search.documents import SearchClient
from fastapi import APIRouter, Depends, HTTPException
from openai import AzureOpenAI
from pydantic import BaseModel

from colep_ai.core.logger import get_logger
from colep_ai.retrieval.ai_search_retrieval import retrieve, RetrievalRejected
from colep_ai.generation.ai_search_generation import generate_from_retrieval
from colep_ai.api.dependencies import get_openai, get_claude, get_search

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
    openai_client: AzureOpenAI = Depends(get_openai),
    claude_client: anthropic.Anthropic = Depends(get_claude),
    search_client: SearchClient = Depends(get_search),
):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    request_start = time.monotonic()

    # --- retrieval ---
    stage_start = time.monotonic()
    retrieval_response = retrieve(
        query=req.query,
        openai_client=openai_client,
        search_client=search_client,
        top_k=req.top_k,
    )
    _log_stage("retrieval", stage_start)

    if isinstance(retrieval_response, RetrievalRejected):
        raise HTTPException(status_code=400, detail=retrieval_response.reason)

    # --- generation ---
    stage_start = time.monotonic()
    generation_output = generate_from_retrieval(
        claude_client=claude_client,
        query=req.query,
        retrieval_response=retrieval_response,
    )
    _log_stage("generation", stage_start)
        
    # --- citation resolution ---
    # results are flat dicts — no nested "payload" wrapper
    stage_start = time.monotonic()
    citations = [
        Citation(
            marker=c["marker"],
            image_url=_resolve_image_url(c["image_ref"], c["source_file"], c["page_number"]),
            image_description=c.get("image_description", ""),
        )
        for c in generation_output["citations"]
    ]
    logger.info(f"citation markers: {[c['marker'] for c in generation_output['citations']]}")
    _log_stage("citation_resolution", stage_start)

    _log_stage("total_request", request_start)

    return QueryResponse(
        answer=generation_output["answer"],
        citations=citations,
        language=generation_output["language"],
    )