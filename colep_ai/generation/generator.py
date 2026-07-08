import anthropic

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

GENERATION_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a factory floor assistant answering questions about Standard Operating Procedures (SOPs).

Rules:
- Answer ONLY using the provided context chunks. Never invent steps, values, or image references not present in context.
- Respond in the same language as the user's question (Portuguese or English).
- If context does not contain enough information to answer, say so explicitly — do not guess.
- When referencing a step, cite its entry_id and page_number.
- List relevant image_ids for any step you reference, exactly as given in context.
"""


def _build_context_block(chunks: list[dict]) -> str:
    lines = []
    for c in chunks:
        lines.append(
            f"[page {c['page_number']} | entry {c['entry_id']}] "
            f"{c['entry_text']} / {c['entry_text_en']} "
            f"(images: {c['image_ids']})"
        )
    return "\n".join(lines)


def generate_answer(query: str, retrieved_chunks: list[dict], client: anthropic.Anthropic) -> dict:
    if not retrieved_chunks:
        return {
            "answer": "No relevant SOP content found for this question.",
            "used_image_ids": [],
            "sources": [],
        }

    context_block = _build_context_block(retrieved_chunks)

    response = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context_block}\n\nQuestion: {query}"
        }],
    )

    answer_text = "".join(
        block.text for block in response.content if block.type == "text"
    )

    used_image_ids = list({img_id for c in retrieved_chunks for img_id in c.get("image_ids", [])})

    logger.info(f"Generated answer for query='{query}' using {len(retrieved_chunks)} chunks")

    return {
        "answer": answer_text,
        "used_image_ids": used_image_ids,
        "sources": [
            {"entry_id": c["entry_id"], "page_number": c["page_number"], "source_file": c["source_file"]}
            for c in retrieved_chunks
        ],
    }