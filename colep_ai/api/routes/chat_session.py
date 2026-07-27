"""
api/routes/chat.py

Chat endpoint with conversation history support.

Flow per request
----------------
1.  Validate session_id (required query param)
2.  Load session state from Redis (hot) → Cosmos (resume) → new
3.  _check_query: classify intent — no history injected (classifier only)
4.  If intent is greeting/malicious → return direct reply, persist user+assistant turns
5.  If intent is conversation_summary → return summary from DB (or generate if none), persist
6.  Retrieval → generation with history_messages injected
7.  Persist user turn (Redis + Cosmos) — blocks
8.  Return response to client
9.  Background task: persist assistant turn → detect eviction →
    update summary → write summary to Redis + Cosmos

Why persist assistant turn in background?
    The client doesn't need to wait for the assistant turn write.
    Cosmos write latency (10–50ms) would otherwise add to p99.
    The user turn is written before responding so it's always safe
    even if the background task fails — the user's question is never lost.
"""

import time
import uuid
import anthropic
import redis.asyncio as aioredis
from azure.search.documents import SearchClient
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from openai import AsyncAzureOpenAI
from pydantic import BaseModel
import asyncio

from colep_ai.core.logger import get_logger
from colep_ai.retrieval.ai_search_retrieval import retrieve, RetrievalRejected
from colep_ai.generation.ai_search_generation_session import generate_from_retrieval
from colep_ai.generation.prompts.check_query_prompt import CHECK_QUERY_SYSTEM_PROMPT
from colep_ai.api.dependencies import get_openai, get_claude, get_search, get_redis, get_cosmos
from colep_ai.conversation.history import (
    SessionState,
    load_or_create_session,
    save_turn,
    build_history_messages,
)
from colep_ai.conversation.summariser import update_summary_incremental
from colep_ai.database.redis_client import set_summary
from colep_ai.database.mongo_client import update_summary as cosmos_update_summary
from colep_ai.database.mongo_client import append_turn as cosmos_append_turn

logger = get_logger(__name__)
router = APIRouter(tags=["Chat"])

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str
    top_k: int = 5
    user_id: str = ""
    user_name: str = ""


class Citation(BaseModel):
    marker: str
    image_url: str | None
    image_description: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    language: str
    session_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_stage(stage: str, start: float) -> None:
    logger.info(f"Stage {stage} | elapsed={time.monotonic() - start:.2f}s")


def _resolve_image_url(
    image_ref: str | None, source_file: str, page_number: int
) -> str | None:
    if not image_ref:
        return None
    if image_ref.endswith(".png") and "_" in image_ref:
        return f"/outputs/{source_file}/combined/page_{page_number}/{image_ref}"
    return f"/outputs/{source_file}/crops/page_{page_number}/crops/{image_ref}.png"


async def _check_query(query: str, openai_client: AsyncAzureOpenAI) -> dict:
    import json

    response = await openai_client.chat.completions.create(
        model="gpt-5.1",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": CHECK_QUERY_SYSTEM_PROMPT,
            },
            {"role": "user", "content": query},
        ],
    )
    return json.loads(response.choices[0].message.content)


async def _resolve_conversation_summary(
    state: SessionState,
    session_id: str,
    openai_client: AsyncAzureOpenAI,
    redis: aioredis.Redis,
    cosmos_container,
) -> str:
    # Case 1: summary exists in DB — return directly, zero LLM calls
    if state.summary.strip():
        logger.info(f"Returning cached summary | session_id={session_id}")
        return state.summary

    # Case 2: no summary yet but turns exist — generate, persist, return
    if state.turns:
        all_turns = "\n".join(
            f"{t['role'].capitalize()}: {t['content']}" for t in state.turns
        )
        generated = await update_summary_incremental(
            openai_client=openai_client,
            existing_summary="",
            user_message=all_turns,
            assistant_message="",
        )
        await set_summary(redis, session_id, generated)
        await cosmos_update_summary(cosmos_container, session_id, generated)
        logger.info(f"Summary generated on demand | session_id={session_id}")
        return generated

    # Case 3: brand new session, nothing to summarise
    return "No topics have been discussed in this session yet."


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def _persist_assistant_and_summarise(
    session_id: str,
    turn_number: int,
    assistant_content: str,
    current_summary: str,
    redis: aioredis.Redis,
    cosmos_container,
    openai_client: AsyncAzureOpenAI,
) -> None:
    """
    Runs after the response is sent to the client.

    Steps:
    1. Push assistant message to Redis → collect evicted items
    2. Persist assistant turn to Cosmos
    3. If eviction occurred AND we have a complete evicted user+assistant pair
       → update summary incrementally
    4. Write updated summary to Redis + Cosmos
    """
    try:
        # 1. Push assistant turn to Redis
        evicted = await _push_and_cosmos(
            session_id, turn_number, "assistant", assistant_content,
            redis, cosmos_container
        )

        if evicted and evicted[0].get("role") == "assistant":
            pending_user = await redis.get(f"session:{session_id}:pending_evicted_user")
            evicted_assistant = evicted[0]["content"]

            if pending_user:
                updated_summary = await update_summary_incremental(
                    openai_client=openai_client,
                    existing_summary=current_summary,
                    user_message=pending_user,
                    assistant_message=evicted_assistant,
                )
                await set_summary(redis, session_id, updated_summary)
                await cosmos_update_summary(cosmos_container, session_id, updated_summary)
                await redis.delete(f"session:{session_id}:pending_evicted_user")
                logger.info(f"Summary updated after eviction | session_id={session_id}")

        elif evicted and evicted[0].get("role") == "user":
            await redis.setex(
                f"session:{session_id}:pending_evicted_user",
                settings_ttl(),
                evicted[0]["content"],
            )

    except Exception as exc:
        logger.exception(
            f"Background task failed | session_id={session_id} | error={exc}"
        )


async def _push_and_cosmos(
    session_id: str,
    turn_number: int,
    role: str,
    content: str,
    redis: aioredis.Redis,
    cosmos_container,
) -> list[dict]:
    """Pushes to Redis and persists to Cosmos. Returns evicted items."""
    from colep_ai.database.redis_client import push_turn
    evicted = await push_turn(redis, session_id, turn_number, role, content)
    await cosmos_append_turn(cosmos_container, session_id, turn_number, role, content)
    return evicted


def settings_ttl() -> int:
    from colep_ai.core.config import settings
    return settings.SESSION_TTL_SECONDS


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@router.post("/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    background_tasks: BackgroundTasks,
    session_id: str = Query(
        default=None,
        description=(
            "Session ID for conversation continuity. "
            "Omit or pass null to start a new session — "
            "the server will generate and return one."
        ),
    ),
    openai_client: AsyncAzureOpenAI = Depends(get_openai),
    claude_client: anthropic.AsyncAnthropic = Depends(get_claude),
    search_client: SearchClient = Depends(get_search),
    redis: aioredis.Redis = Depends(get_redis),
    cosmos_container=Depends(get_cosmos),
):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    if not session_id:
        session_id = str(uuid.uuid4())
        logger.info(f"New session_id generated | session_id={session_id}")

    request_start = time.monotonic()

    # ------------------------------------------------------------------
    # 1. Load session state
    # ------------------------------------------------------------------
    stage_start = time.monotonic()
    state = await load_or_create_session(
        session_id, redis, cosmos_container,
        user_id=req.user_id, user_name=req.user_name,
    )
    _log_stage("session_load", stage_start)

    # ------------------------------------------------------------------
    # 2. Persist user turn (blocking)
    # ------------------------------------------------------------------
    next_turn_number = state.turn_count + 1

    stage_start = time.monotonic()
    user_evicted = await save_turn(
        session_id=session_id,
        turn_number=next_turn_number,
        role="user",
        content=req.query,
        redis=redis,
        cosmos_container=cosmos_container,
    )
    _log_stage("user_turn_save", stage_start)

    if user_evicted and user_evicted[0].get("role") == "user":
        await redis.setex(
            f"session:{session_id}:pending_evicted_user",
            settings_ttl(),
            user_evicted[0]["content"],
        )

    # ------------------------------------------------------------------
    # 3. Classify query
    # ------------------------------------------------------------------
    stage_start = time.monotonic()
    check = await _check_query(req.query, openai_client)
    intent = check.get("intent")
    _log_stage("check_query", stage_start)

    # ------------------------------------------------------------------
    # 4a. Greeting / malicious — reply directly
    # ------------------------------------------------------------------
    if intent in ("greeting", "malicious"):
        assistant_reply = check["reply"]
        background_tasks.add_task(
            _persist_assistant_and_summarise,
            session_id=session_id,
            turn_number=next_turn_number,
            assistant_content=assistant_reply,
            current_summary=state.summary,
            redis=redis,
            cosmos_container=cosmos_container,
            openai_client=openai_client,
        )
        return QueryResponse(answer=assistant_reply, citations=[], language="", session_id=session_id)

    # ------------------------------------------------------------------
    # 4b. Conversation summary — serve from DB or generate once
    # ------------------------------------------------------------------
    if intent == "conversation_summary":
        assistant_reply = await _resolve_conversation_summary(
            state=state,
            session_id=session_id,
            openai_client=openai_client,
            redis=redis,
            cosmos_container=cosmos_container,
        )
        background_tasks.add_task(
            _persist_assistant_and_summarise,
            session_id=session_id,
            turn_number=next_turn_number,
            assistant_content=assistant_reply,
            current_summary=state.summary,
            redis=redis,
            cosmos_container=cosmos_container,
            openai_client=openai_client,
        )
        return QueryResponse(answer=assistant_reply, citations=[], language="", session_id=session_id)

    # ------------------------------------------------------------------
    # 5. Retrieval
    # ------------------------------------------------------------------
    stage_start = time.monotonic()
    retrieval_response = await retrieve(
        query=req.query,
        openai_client=openai_client,
        search_client=search_client,
        top_k=req.top_k,
    )
    _log_stage("retrieval", stage_start)

    if isinstance(retrieval_response, RetrievalRejected):
        raise HTTPException(status_code=400, detail=retrieval_response.reason)

    # ------------------------------------------------------------------
    # 6. Build history messages for generation
    # ------------------------------------------------------------------
    history_messages = build_history_messages(state)

    # ------------------------------------------------------------------
    # 7. Generation
    # ------------------------------------------------------------------
    stage_start = time.monotonic()
    try:
        generation_output = await generate_from_retrieval(
            claude_client=claude_client,
            query=req.query,
            retrieval_response=retrieval_response,
            history_messages=history_messages,
        )
    except anthropic.RateLimitError:
        logger.warning(f"Anthropic rate limit — returning 503 | session_id={session_id}")
        raise HTTPException(
            status_code=503,
            detail="Service is currently busy. Please try again in a moment.",
        )
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
        logger.error(f"Anthropic upstream unavailable after retries | error={exc} | session_id={session_id}")
        raise HTTPException(
            status_code=503,
            detail="Upstream service unavailable. Please try again shortly.",
        )
    _log_stage("generation", stage_start)

    # ------------------------------------------------------------------
    # 8. Citation resolution
    # ------------------------------------------------------------------
    stage_start = time.monotonic()
    citations = [
        Citation(
            marker=c["marker"],
            image_url=_resolve_image_url(
                c["image_ref"], c["source_file"], c["page_number"]
            ),
            image_description=c.get("image_description", ""),
        )
        for c in generation_output["citations"]
    ]
    logger.info(f"citation markers: {[c['marker'] for c in generation_output['citations']]}")
    _log_stage("citation_resolution", stage_start)

    # ------------------------------------------------------------------
    # 9. Persist assistant turn + summarise in background
    # ------------------------------------------------------------------
    background_tasks.add_task(
        _persist_assistant_and_summarise,
        session_id=session_id,
        turn_number=next_turn_number,
        assistant_content=generation_output["answer"],
        current_summary=state.summary,
        redis=redis,
        cosmos_container=cosmos_container,
        openai_client=openai_client,
    )

    _log_stage("total_request", request_start)

    return QueryResponse(
        answer=generation_output["answer"],
        citations=citations,
        language=generation_output["language"],
        session_id=session_id,
    )