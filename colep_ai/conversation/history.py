"""
conversation/history.py

History manager — the single orchestration layer that coordinates
Redis (hot state), Cosmos (persistence), and the summariser.

All history operations in the chat route go through this module.
The route never touches Redis or Cosmos directly.

Public API
----------
load_or_create_session(session_id, redis, cosmos)
    → SessionState

save_turn(session_id, turn_number, role, content, redis, cosmos)
    → list[dict]  (evicted items, if any)

build_history_messages(state)
    → list[dict]  (Claude-formatted messages list)

SessionState
------------
A plain dataclass holding the hot state for one request:
    summary: str        — running summary of evicted turns
    turns:   list[dict] — last N turns from Redis
    turn_count: int     — total turns so far (for numbering new turns)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import redis.asyncio as aioredis

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
# from colep_ai.database import cosmos_client as cosmos
from colep_ai.database import mongo_client as cosmos
from colep_ai.database import redis_client as rclient

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    summary: str = ""
    turns: list[dict] = field(default_factory=list)
    turn_count: int = 0          # total persisted turns (used to number next turn)
    is_new: bool = False         # True if session was created this request


# ---------------------------------------------------------------------------
# Load or create
# ---------------------------------------------------------------------------

async def load_or_create_session(
    session_id: str,
    redis: aioredis.Redis,
    cosmos_container,
    user_id: str = "",
    user_name: str = "",
) -> SessionState:
    """
    1. Try Redis first (hot path — most requests land here)
    2. On miss → try Cosmos (resume path)
    3. On Cosmos miss → new session

    Returns a SessionState ready for the current request.
    """
    # --- hot path ---
    redis_state = await rclient.get_session_state(redis, session_id)
    if redis_state is not None:
        logger.info(f"Session loaded from Redis | session_id={session_id}")
        return SessionState(
            summary=redis_state["summary"],
            turns=redis_state["turns"],
            turn_count=_count_turns(redis_state["turns"]),
        )

    # --- resume path ---
    cosmos_state = await cosmos.load_session(cosmos_container, session_id)
    if cosmos_state is not None:
        # Rehydrate Redis from Cosmos
        await rclient.seed_session(
            redis,
            session_id,
            turns=cosmos_state["turns"],
            summary=cosmos_state["summary"],
        )
        logger.info(f"Session resumed from Cosmos | session_id={session_id}")
        return SessionState(
            summary=cosmos_state["summary"],
            turns=cosmos_state["turns"][-settings.SESSION_HISTORY_WINDOW * 2:],
            turn_count=_count_turns(cosmos_state["turns"]),
        )

    # --- new session ---
    await cosmos.create_session(cosmos_container, session_id, user_id=user_id, user_name=user_name)
    await rclient.init_session(redis, session_id)
    logger.info(f"New session created | session_id={session_id}")
    return SessionState(is_new=True)


# ---------------------------------------------------------------------------
# Save turn (called twice per request: once for user, once for assistant)
# ---------------------------------------------------------------------------

async def save_turn(
    session_id: str,
    turn_number: int,
    role: str,
    content: str,
    redis: aioredis.Redis,
    cosmos_container,
) -> list[dict]:
    """
    Persists a single message to both Redis and Cosmos.
    Returns evicted items from Redis (empty list if no eviction).
    """
    # Redis push returns evicted items
    evicted = await rclient.push_turn(
        redis, session_id, turn_number, role, content
    )

    # Cosmos is fire-and-forget friendly but we await here for correctness.
    # In the route, Cosmos writes for the assistant turn happen in the
    # background task alongside summarisation to avoid blocking response.
    await cosmos.append_turn(
        cosmos_container, session_id, turn_number, role, content
    )

    return evicted


# ---------------------------------------------------------------------------
# Build Claude messages list from session state
# ---------------------------------------------------------------------------

def build_history_messages(state: SessionState) -> list[dict]:
    """
    Converts SessionState into the messages list for Claude's API.

    Structure injected into the messages array:
    [
        # If summary exists — injected as a user/assistant exchange
        # so it fits Claude's alternating turn requirement
        {"role": "user",      "content": "[Conversation summary for context]:\n<summary>"},
        {"role": "assistant", "content": "Understood. I will use this context."},

        # Then last N verbatim turns
        {"role": "user",      "content": "<user turn N-2>"},
        {"role": "assistant", "content": "<assistant turn N-2>"},
        ...
    ]

    The current query is NOT included here — the route appends it last.

    Why inject summary as a user/assistant pair?
        Claude's messages API requires strict alternating roles.
        A bare system-level summary block would break that invariant
        if the first real turn is also from the user. Wrapping it in
        a synthetic exchange is the cleanest way to satisfy the API
        without restructuring the generation call.
    """
    messages: list[dict] = []

    if state.summary.strip():
        messages.append({
            "role": "user",
            "content": (
                "[Previous conversation summary — use as background context only. "
                "Do not reference this summary directly in your answer unless asked.]\n\n"
                f"{state.summary}"
            ),
        })
        messages.append({
            "role": "assistant",
            "content": "Understood. I have the conversation context.",
        })

    for turn in state.turns:
        messages.append({
            "role": turn["role"],
            "content": turn["content"],
        })

    return messages


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _count_turns(turns: list[dict]) -> int:
    """
    Derives the highest turn_number seen so far.
    Used to correctly number the next turn.
    """
    if not turns:
        return 0
    return max(t.get("turn_number", 0) for t in turns)