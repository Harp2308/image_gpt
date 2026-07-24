"""
api/routes/history.py

GET /history/{session_id}

Returns full conversation history for a session:
- All turns grouped as user + assistant pairs
- Running summary (generated fresh via GPT-5.1 if not present)
- Session metadata
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from openai import AzureOpenAI
from pydantic import BaseModel

from colep_ai.api.dependencies import get_openai, get_cosmos
from colep_ai.database import mongo_client as db
from colep_ai.core.logger import get_logger
from colep_ai.generation.prompts.chat_summary_prompt import SUMMARY_SYSTEM_PROMPT
logger = get_logger(__name__)
router = APIRouter(tags=["History"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TurnPair(BaseModel):
    turn_number: int
    user: str
    assistant: str
    timestamp: str          # timestamp of the assistant message (end of turn)


class HistoryResponse(BaseModel):
    session_id: str
    total_turns: int
    summary: str
    turns: list[TurnPair]


# ---------------------------------------------------------------------------
# Summary generation via GPT-5.1
# ---------------------------------------------------------------------------

def _generate_summary(
    turns: list[TurnPair],
    openai_client: AzureOpenAI,
) -> str:
    """
    Generates a fresh summary of the full conversation using GPT-5.1.
    Called only when no summary exists yet in MongoDB.
    """
    if not turns:
        return ""

    conversation_text = "\n\n".join(
        f"User: {t.user}\nAssistant: {t.assistant}"
        for t in turns
    )

    response = openai_client.chat.completions.create(
    model="gpt-5.1",
    temperature=0,
    messages=[
        {
            "role": "system",
            "content": SUMMARY_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": conversation_text,
        },
    ],
)

    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/history/{session_id}", response_model=HistoryResponse)
async def get_history(
    session_id: str,
    openai_client: AzureOpenAI = Depends(get_openai),
    cosmos_container=Depends(get_cosmos),
):
    # Load full session from MongoDB
    session_data = await db.load_session(cosmos_container, session_id)

    if session_data is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session '{session_id}' not found.",
        )

    raw_turns = session_data["turns"]
    summary = session_data.get("summary", "")

    # Group flat turn list into user + assistant pairs by turn_number
    pairs: dict[int, dict] = {}
    for msg in raw_turns:
        tn = msg["turn_number"]
        if tn not in pairs:
            pairs[tn] = {"user": "", "assistant": "", "timestamp": ""}
        pairs[tn][msg["role"]] = msg["content"]
        if msg["role"] == "assistant":
            pairs[tn]["timestamp"] = msg.get("timestamp", "")

    turn_pairs = [
        TurnPair(
            turn_number=tn,
            user=data["user"],
            assistant=data["assistant"],
            timestamp=data["timestamp"],
        )
        for tn, data in sorted(pairs.items())
    ]

    # Generate summary via GPT-5.1 if not present
    if not summary.strip() and turn_pairs:
        logger.info(f"No summary found — generating via GPT-5.1 | session_id={session_id}")
        summary = _generate_summary(turn_pairs, openai_client)
        # Persist generated summary back to MongoDB
        await db.update_summary(cosmos_container, session_id, summary)

    return HistoryResponse(
        session_id=session_id,
        total_turns=len(turn_pairs),
        summary=summary,
        turns=turn_pairs,
    )