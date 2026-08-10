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
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from openai import AsyncAzureOpenAI
from pydantic import BaseModel
import asyncio

from colep_ai.core.logger import get_logger
from colep_ai.retrieval.ai_search_retrieval import retrieve, RetrievalRejected,RetrievalResponse
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
# from colep_ai.database.mongo_client import update_summary as cosmos_update_summary
# from colep_ai.database.mongo_client import append_turn as cosmos_append_turn
from colep_ai.database.cosmos_client import update_summary as cosmos_update_summary
from colep_ai.database.cosmos_client import append_turn as cosmos_append_turn

from colep_ai.database.query_log_client import get_query_logs_container, write_query_log
from colep_ai.core.config import settings as _settings
from colep_ai.retrieval.reranker import rerank

logger = get_logger(__name__)
router = APIRouter(tags=["Chat"])

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str
    top_k: int = 20
    user_id: str = ""
    user_name: str = ""


class Citation(BaseModel):
    marker: str
    source_file: str
    image_url: str | None
    pdf_url: str | None = None
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

def _resolve_image_blob_url(
    image_ref: str | None, source_file: str, page_number: int, folder_name: str = ""
) -> str | None:
    if not image_ref:
        return None
    prefix = f"{folder_name}/{source_file}" if folder_name else source_file
    if image_ref.endswith(".png") and "_" in image_ref:
        return f"/blob/view/{prefix}/combined/page_{page_number}/{image_ref}"
    return f"/blob/view/{prefix}/crops/page_{page_number}/crops/{image_ref}.png"

def _resolve_pdf_blob_url(source_file: str, folder_name: str = "") -> str:
    prefix = f"{folder_name}/{source_file}" if folder_name else source_file
    return f"/blob/view/{prefix}/pdf/{source_file}.pdf"

async def _check_query(
    query: str,
    openai_client: AsyncAzureOpenAI,
    history_turns: list[dict],          
) -> dict:
    import json

    # Build history block for classifier context
    history_text = ""
    if history_turns:
        lines = []
        for t in history_turns:
            role_label = "User" if t["role"] == "user" else "Assistant"
            lines.append(f"{role_label}: {t['content']}")
        history_text = "\n\n".join(lines)

    user_content = (
        f"Conversation history (last {len(history_turns)} turns):\n{history_text}\n\nCurrent query: {query}"
        if history_text
        else f"Current query: {query}"
    )

    response = await openai_client.chat.completions.create(
        model="gpt-5.1",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CHECK_QUERY_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    result = json.loads(response.choices[0].message.content)
    result["_tokens"] = {
        "input": response.usage.prompt_tokens,
        "output": response.usage.completion_tokens,
    }
    return result

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
# New background task — logging only, no impact on response path
# ---------------------------------------------------------------------------

async def _log_query_background(
    session_id: str,
    ip: str,
    user_agent: str,
    question: str,
    answer: str,
    intent: str,
    language: str = "",
    model: str = "",
    context: str | None = None,
    tokens: dict | None = None,
    classifier_response: dict | None = None,
) -> None:
    """
    Writes query log to Cosmos in background.
    Failures are swallowed — logging must never affect the response path.
    """
    try:
        container = get_query_logs_container()
        await write_query_log(
            container=container,
            session_id=session_id,
            ip=ip,
            user_agent=user_agent,
            question=question,
            answer=answer,
            intent=intent,
            language=language,
            model=model,
            context=context,
            tokens=tokens,
            classifier_response=classifier_response,
        )
    except Exception as exc:
        from colep_ai.core.logger import get_logger
        get_logger(__name__).warning(f"Query log write failed (non-fatal) | {exc}")


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@router.post("/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    request: Request,                                           # <-- ADDED
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

    # Capture IP and user-agent once at route entry
    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    user_agent = request.headers.get("User-Agent", "unknown")

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
    history_turns = state.turns[-3:] if state.turns else []        
    check = await _check_query(req.query, openai_client, history_turns)
    intent = check.get("intent")
    classifier_response = {k: v for k, v in check.items() if k != "_tokens"}
    logger.info(f"Classifier result | {classifier_response}")
    _log_stage("check_query", stage_start)

    # ------------------------------------------------------------------
    # 4a. Greeting / malicious — reply directly
    # ------------------------------------------------------------------
    if intent in ("greeting", "malicious"):
        assistant_reply = check["reply"]
        


        # Log non-retrieval path (no context — retrieval never ran)
        background_tasks.add_task(
            _log_query_background,
            session_id=session_id,
            ip=client_ip,
            user_agent=user_agent,
            question=req.query,
            answer=assistant_reply,
            intent=intent,
            language="",
            model="gpt-5.1",        # classifier model
            context=None,
            classifier_response=classifier_response,
            tokens={
                "classifier_input":    check["_tokens"]["input"],
                "classifier_output":   check["_tokens"]["output"],
                "generation_input":    None,
                "generation_output":   None,
            },
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

        return QueryResponse(
            answer=assistant_reply,
            citations=[],
            language="",
            session_id=session_id,
        )
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
    is_followup = check.get("is_followup", False)
    effective_query = (
        check["rephrased_query"]
        if is_followup and check.get("rephrased_query")
        else req.query
    )
    source_file_filter = (
        check.get("followup_source_file") or None
        if is_followup
        else None
    )

    if is_followup:
        logger.info(
            f"Follow-up detected | rephrased='{effective_query}' "
            f"| source_file_filter={source_file_filter} "
            f"| line_override={check.get('followup_line_number')}"
        )

    retrieval_response = await retrieve(
        query=effective_query,
        openai_client=openai_client,
        search_client=search_client,
        top_k=20,
        source_file_filter=source_file_filter,
    )
    _log_stage("retrieval", stage_start)
    
    if isinstance(retrieval_response, RetrievalRejected):
        return QueryResponse(
            answer=retrieval_response.reason,
            citations=[],
            language="",
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # 5b. Rerank — skip if query is already line-filtered (retrieval is precise)
    # ------------------------------------------------------------------
    # if not retrieval_response.line_filter:
    stage_start = time.monotonic()
    retrieval_response = RetrievalResponse(
        results=await rerank(req.query, retrieval_response.results),
        language=retrieval_response.language,
        line_filter=retrieval_response.line_filter,
    )
    _log_stage("rerank", stage_start)

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
            source_file=c["source_file"],
            image_url=_resolve_image_blob_url(
                c["image_ref"],
                c["source_file"],
                c["page_number"],
                c.get("folder_name", ""),
            ),
            pdf_url=_resolve_pdf_blob_url(
                c["source_file"],
                c.get("folder_name", ""),
            ),
            image_description=c.get("image_description", ""),
        )
        for c in generation_output["citations"]
    ]
    logger.info(f"citation markers: {[c['marker'] for c in generation_output['citations']]}")
    _log_stage("citation_resolution", stage_start)

    # ------------------------------------------------------------------
    # 9. Log retrieval path — full context included
    # ------------------------------------------------------------------
    background_tasks.add_task(
        _log_query_background,
        session_id=session_id,
        ip=client_ip,
        user_agent=user_agent,
        question=req.query,
        answer=generation_output["answer"],
        intent=intent,
        language=generation_output["language"],
        model=_settings.ANTHROPIC_MODEL,
       context=generation_output.get("context"),
        classifier_response=classifier_response,
        tokens={
            "classifier_input":  check["_tokens"]["input"],
            "classifier_output": check["_tokens"]["output"],
            "generation_input":  generation_output["generation_tokens"]["input"],
            "generation_output": generation_output["generation_tokens"]["output"],
        },
    )

    # ------------------------------------------------------------------
    # 10. Persist assistant turn + summarise in background
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
