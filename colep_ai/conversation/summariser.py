"""
conversation/summariser.py

Incremental conversation summariser.

Called as a FastAPI BackgroundTask after every turn eviction.
Folds one evicted turn pair (user + assistant) into the existing
running summary — cheap, incremental, never recomputes from scratch.

Design decisions
----------------
- Uses Claude (claude-sonnet-4-6) — same model as generation,
  consistent quality, already initialised in the app.
- Synchronous Claude call inside an async background task via
  asyncio.to_thread — avoids blocking the event loop.
- The summary is intentionally brief: it captures what was asked
  and what was answered, not the full verbatim exchange.
  This keeps the summary token count bounded even over many turns.
- On failure: logs the error and returns the existing summary unchanged.
  A failed summarisation is recoverable — the session continues
  with a slightly stale summary rather than crashing.
"""

from __future__ import annotations

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from openai import AsyncAzureOpenAI
logger = get_logger(__name__)

_SUMMARY_SYSTEM_PROMPT = """\
You are a conversation summariser for an industrial factory assistant.
Your job is to maintain a running summary of a conversation between an
operator and the factory AI assistant.

You will receive:
1. The existing summary (may be empty for the first eviction)
2. A user question that is being evicted from the active window
3. The assistant's answer to that question

Produce an updated summary that:
- Incorporates the new question and answer into the existing summary
- Stays concise — one to three sentences maximum
- Preserves the most operationally relevant information
  (procedures discussed, machine names, step numbers, document codes)
- Drops conversational filler and greetings
- Is written in the same language as the conversation

Return ONLY the updated summary text. No preamble, no labels.\
"""


def _build_summary_prompt(
    existing_summary: str,
    user_message: str,
    assistant_message: str,
) -> str:
    parts = []
    if existing_summary.strip():
        parts.append(f"Existing summary:\n{existing_summary}")
    else:
        parts.append("Existing summary: (none)")

    parts.append(f"User question being evicted:\n{user_message}")
    parts.append(f"Assistant answer being evicted:\n{assistant_message}")
    parts.append("Produce the updated summary:")
    return "\n\n".join(parts)


async def _call_summariser(
     openai_client: AsyncAzureOpenAI,
    existing_summary: str,
    user_message: str,
    assistant_message: str,
) -> str:
    """
    Synchronous Claude call — runs in a thread via asyncio.to_thread.
    Returns the updated summary string.
    """
    prompt = _build_summary_prompt(existing_summary, user_message, assistant_message)

    resp = await openai_client.chat.completions.create(
    model=settings.SUMMARY_MODEL,
    max_completion_tokens=3000,
    temperature=0,
    messages=[
        {
            "role": "system",
            "content": _SUMMARY_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": prompt,
        },
    ],
)
    updated = resp.choices[0].message.content.strip()
    logger.debug(
    f"Summary updated | "
    f"prompt_tokens={resp.usage.prompt_tokens}, "
    f"completion_tokens={resp.usage.completion_tokens}, "
    f"total_tokens={resp.usage.total_tokens}"
)
    return updated


async def update_summary_incremental(
     openai_client: AsyncAzureOpenAI,
    existing_summary: str,
    user_message: str,
    assistant_message: str,
) -> str:
    """
    Async wrapper — offloads the blocking Claude SDK call to a thread.
    Returns updated summary, or existing_summary on failure.
    """
    try:
        updated = await _call_summariser(
            openai_client,
            existing_summary,
            user_message,
            assistant_message,
        )
        return updated
    except Exception as exc:
        logger.exception(
            f"Summarisation failed — keeping existing summary | error={exc}"
        )
        return existing_summary