"""
FastAPI: query -> retrieve -> generate -> resolve images -> return to frontend.
"""
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel
from qdrant_client import QdrantClient

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.retrieval.page_retrieval import retrieve
from colep_ai.generation.answer_builder import generate_from_retrieval
from colep_ai.database.qdrant_page_client import get_qdrant_client

logger = get_logger(__name__)

app = FastAPI(title="Colep AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve outputs/ as static so frontend can load images via URL
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")

# --------------------------------------------------------------------------
# Clients — initialized once at startup, reused across requests.
# --------------------------------------------------------------------------
_openai_client: OpenAI | None = None
_claude_client: anthropic.Anthropic | None = None
_qdrant_client: QdrantClient | None = None


@app.on_event("startup")
def _init_clients():
    global _openai_client, _claude_client, _qdrant_client
    _openai_client = OpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())
    _claude_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())
    _qdrant_client = get_qdrant_client()
    logger.info("Clients initialized")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
class QueryRequest(BaseModel):
    query: str
    top_k: int = 5


class Citation(BaseModel):
    marker: str
    image_url: str | None  # resolved full URL, None if no image
    image_description: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    language: str


# --------------------------------------------------------------------------
# Image URL resolution
# --------------------------------------------------------------------------
def _resolve_image_url(image_ref: str | None, source_file: str, page_number: int) -> str | None:
    """
    image_ref is either:
      - a combined filename like 'page_3_11_a2c9.png'  -> outputs/{source_file}/combined/page_{n}/{filename}
      - a raw crop id like 'page_2_9347'               -> outputs/{source_file}/crops/page_{n}/crops/{id}.png
    Returns a URL path the frontend can fetch via /outputs/...
    """
    if not image_ref:
        return None

    # Combined images already have .png extension and contain entry_id in name
    if image_ref.endswith(".png") and "_" in image_ref:
        path = f"/outputs/{source_file}/combined/page_{page_number}/{image_ref}"
    else:
        # Raw crop id — no extension
        path = f"/outputs/{source_file}/crops/page_{page_number}/crops/{image_ref}.png"

    return path


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------
@app.post("/query", response_model=QueryResponse,tags=["Chat"])
def query(req: QueryRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    retrieval_response = retrieve(
        query=req.query,
        openai_client=_openai_client,
        qdrant_client=_qdrant_client,
        top_k=req.top_k,
    )

    generation_output = generate_from_retrieval(
        claude_client=_claude_client,
        query=req.query,
        retrieval_response=retrieval_response,
    )

    # Resolve image URLs — need source_file + page_number per citation.
    # Build a flat lookup from marker -> (source_file, page_number) from results.
    marker_meta: dict[str, tuple[str, int]] = {}
    for result in retrieval_response.results:
        payload = result["payload"]
        source_file = payload.get("source_file", "")
        page_number = payload.get("page_number", 0)
        for idx, entry in enumerate(payload.get("entries", []), start=1):
            marker_key = f"[page {page_number} | entry {idx}]"
            marker_meta[marker_key] = (source_file, page_number)

    citations: list[Citation] = []
    for c in generation_output["citations"]:
        source_file, page_number = marker_meta.get(c["marker"], ("", 0))
        image_url = _resolve_image_url(c["image_ref"], source_file, page_number)
        citations.append(Citation(
            marker=c["marker"],
            image_url=image_url,
            image_description=c.get("image_description", ""),
        ))

    return QueryResponse(
        answer=generation_output["answer"],
        citations=citations,
        language=generation_output["language"],
    )