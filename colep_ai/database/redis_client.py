"""
database/redis_client.py

Redis client for hot session state.

What lives in Redis per session
--------------------------------
Two keys per session_id:

  session:{session_id}:turns   → Redis List of JSON strings
      Each item: {"turn_number": int, "role": str, "content": str}
      Capped at WINDOW * 2 items (user + assistant per turn).
      LPUSH + LTRIM keeps it bounded and atomic.
      Stored oldest-first (index 0 = oldest).

  session:{session_id}:summary → Redis String
      The running plain-text summary of all evicted turns.
      Empty string if no evictions yet.

Both keys share the same TTL, reset (sliding) on every write.

Why a List and not a JSON blob for turns?
  LPUSH/LTRIM is atomic — no read-modify-write race under
  concurrent requests for the same session. A JSON blob would
  require WATCH/MULTI/EXEC or a Lua script to update safely.
"""

from __future__ import annotations

import json
from typing import Optional

import redis.asyncio as aioredis

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

_TURNS_KEY = "session:{sid}:turns"
_SUMMARY_KEY = "session:{sid}:summary"

# Maximum list items = window size * 2 (user + assistant per turn)
_MAX_ITEMS = settings.SESSION_HISTORY_WINDOW * 2


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def get_redis_client() -> aioredis.Redis:
    """
    Returns an async Redis client.
    Used as a FastAPI dependency via Depends().
    Connection pool is managed by redis-py internally.
    """
    return aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _turns_key(session_id: str) -> str:
    return _TURNS_KEY.format(sid=session_id)

def _summary_key(session_id: str) -> str:
    return _SUMMARY_KEY.format(sid=session_id)


# ---------------------------------------------------------------------------
# Session state operations
# ---------------------------------------------------------------------------

async def get_session_state(
    redis: aioredis.Redis,
    session_id: str,
) -> Optional[dict]:
    """
    Loads current hot state from Redis.

    Returns:
        {
            "turns": [{"turn_number": int, "role": str, "content": str}, ...],
            "summary": str,
        }
    or None if session does not exist in Redis (cache miss → resume from Cosmos).
    """
    turns_raw = await redis.lrange(_turns_key(session_id), 0, -1)

    # Explicit miss check — empty list could mean new session or evicted key
    # We disambiguate by checking both keys exist
    exists = await redis.exists(_turns_key(session_id))
    if not exists:
        return None

    turns = [json.loads(t) for t in turns_raw]
    summary = await redis.get(_summary_key(session_id)) or ""

    return {"turns": turns, "summary": summary}


async def push_turn(
    redis: aioredis.Redis,
    session_id: str,
    turn_number: int,
    role: str,
    content: str,
) -> list[dict]:
    """
    Appends a single message to the turns list.
    Trims to the window cap atomically.
    Resets TTL on both keys.

    Returns the evicted item if the list was at capacity before push,
    else returns an empty list.

    Eviction detection:
        Before push, if list length == _MAX_ITEMS, the oldest item
        (index 0) will be pushed out by LTRIM. We read it before
        pushing so the summariser can fold it in.
    """
    tk = _turns_key(session_id)
    sk = _summary_key(session_id)
    ttl = settings.SESSION_TTL_SECONDS

    # Read current length and oldest item before mutating
    current_len = await redis.llen(tk)
    evicted = []

    if current_len >= _MAX_ITEMS:
        # The two oldest items (one full turn = user + assistant) will be evicted
        # We evict one message at a time — the summariser is called per-message push
        # but will only act when a complete turn pair is evicted.
        oldest_raw = await redis.lindex(tk, 0)
        if oldest_raw:
            evicted = [json.loads(oldest_raw)]

    item = json.dumps({
        "turn_number": turn_number,
        "role": role,
        "content": content,
    })

    # Atomic push + trim + TTL reset
    pipe = redis.pipeline()
    pipe.rpush(tk, item)
    pipe.ltrim(tk, -_MAX_ITEMS, -1)    # keep only last _MAX_ITEMS items
    pipe.expire(tk, ttl)
    pipe.expire(sk, ttl)               # reset summary TTL on every turn too
    await pipe.execute()

    logger.debug(
        f"Turn pushed to Redis | session_id={session_id} "
        f"turn={turn_number} role={role} evicted={len(evicted)}"
    )

    return evicted


async def set_summary(
    redis: aioredis.Redis,
    session_id: str,
    summary: str,
) -> None:
    """Overwrites the running summary and resets TTL."""
    sk = _summary_key(session_id)
    pipe = redis.pipeline()
    pipe.set(sk, summary)
    pipe.expire(sk, settings.SESSION_TTL_SECONDS)
    await pipe.execute()
    logger.debug(f"Summary written to Redis | session_id={session_id}")


async def seed_session(
    redis: aioredis.Redis,
    session_id: str,
    turns: list[dict],
    summary: str,
) -> None:
    """
    Called on resume to rehydrate Redis from Cosmos data.
    Loads only the last SESSION_HISTORY_WINDOW turns (last _MAX_ITEMS messages).
    Sets TTL on both keys.
    """
    tk = _turns_key(session_id)
    sk = _summary_key(session_id)
    ttl = settings.SESSION_TTL_SECONDS

    # Take only the last _MAX_ITEMS messages for the hot window
    recent = turns[-_MAX_ITEMS:]

    pipe = redis.pipeline()
    pipe.delete(tk)     # clear any stale state
    for item in recent:
        pipe.rpush(tk, json.dumps(item))
    pipe.ltrim(tk, -_MAX_ITEMS, -1)
    pipe.expire(tk, ttl)
    pipe.set(sk, summary)
    pipe.expire(sk, ttl)
    await pipe.execute()

    logger.info(
        f"Session seeded in Redis from Cosmos | session_id={session_id} "
        f"items={len(recent)} has_summary={bool(summary)}"
    )


async def init_session(
    redis: aioredis.Redis,
    session_id: str,
) -> None:
    """
    Initialises an empty session in Redis for a brand new session_id.
    Sets an empty summary key so existence checks work correctly.
    """
    tk = _turns_key(session_id)
    sk = _summary_key(session_id)
    ttl = settings.SESSION_TTL_SECONDS

    pipe = redis.pipeline()
    # LPUSH a sentinel then immediately delete it — we just want the key to exist
    # Cleaner: just set the summary key to empty string; turns key will be created on first push
    pipe.set(sk, "")
    pipe.expire(sk, ttl)
    # Create turns key as an empty list via a set + delete trick isn't needed —
    # existence is checked via summary key in get_session_state
    await pipe.execute()

    logger.info(f"New session initialised in Redis | session_id={session_id}")