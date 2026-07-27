"""
Generation layer: turns retrieved page/entry chunks into LLM context and
produces the final answer. Deliberately kept separate from page_retrieval.py
(vector search / fusion) so the two can be tested, versioned, and scaled
independently - e.g. swapping the answer model or prompt without touching
retrieval logic, or mocking generation in retrieval tests and vice versa.
Inline image markers
---------------------
The LLM is instructed to place markers like `🖼️[page 3 | entry 10]` right
after any step in its answer that has an associated image. These markers
are NOT shown to the user as raw text - `extract_citations()` resolves
each one back to the real entry (image_ids / combined_image path) using
the same (page, entry_number) lookup built while formatting context, so
the frontend can render the actual image at that point in the answer
instead of a literal bracketed tag.
"""

import anthropic
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from colep_ai.core.logger import get_logger
from colep_ai.retrieval.page_retrieval import RetrievalResponse
from colep_ai.core.config import settings
from colep_ai.utils.query_log import log_query
import re
logger = get_logger(__name__)

GENERATION_MODEL = settings.ANTHROPIC_MODEL

# Same retry policy shape as page_retrieval.py: only retry transient
# failures, fail fast on anything else (bad request, auth, etc).
_RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)

# Matches markers of the exact form the LLM is instructed to emit:
# 🖼️[page 3 | entry 10]

_IMAGE_MARKER_PATTERN = re.compile(r"🖼️\[page\s+(\d+)\s*\|\s*entry\s+(\d+)\]")
EntryLookup = dict[tuple[int, int], dict]

# v2
def format_context_for_llm(results: list[dict], language: str) -> tuple[str, EntryLookup]:
    use_native_pt_text = language == "portuguese"
    blocks: list[str] = []
    entry_lookup: EntryLookup = {}

    for result in results:
        payload = result.get("payload", {})
        page_number = payload.get("page_number")
        entries = payload.get("entries", [])

        # Legend is page-level — emit once before entries if present
        legend = payload.get("legend", [])
        legend_block = ""
        if legend:
            legend_lines = [f"  {item['symbol']}: {item['meaning']}" for item in legend if item.get("symbol") and item.get("meaning")]
            if legend_lines:
                legend_block = "legend:\n" + "\n".join(legend_lines)

        for idx, entry in enumerate(entries, start=1):
            text = entry.get("entry_text") if use_native_pt_text else entry.get("entry_text_en")
            if not text:
                text = entry.get("entry_text_en") or entry.get("entry_text") or ""
            has_image = bool(entry.get("image_ids") or entry.get("combined_image"))

            lines = [f"[page {page_number} | entry {idx}]"]

            if payload.get("document_title"):
                lines.append(f"document_title: {payload['document_title']}")
            if payload.get("document_code"):
                lines.append(f"document_code: {payload['document_code']}")
            if payload.get("source_file"):
                lines.append(f"source_file: {payload['source_file']}")
            line_number = payload.get("line_number", payload.get("Line_number"))
            if line_number is not None:
                lines.append(f"line_number: {line_number}")

            # Inject legend once, only on the first entry of this page
            if legend_block and idx == 1:
                lines.append(legend_block)

            if text:
                lines.append(f"text: {text}")

            # Fields: flatten key-value, skip empty values
            fields = entry.get("fields", {})
            if fields:
                field_lines = [
                    f"  {k}: {v}" for k, v in fields.items() if v and str(v).strip()
                ]
                if field_lines:
                    lines.append("fields:\n" + "\n".join(field_lines))

            if entry.get("image_description"):
                lines.append(f"image_description: {entry['image_description']}")
            lines.append(f"has_image: {'yes' if has_image else 'no'}")

            blocks.append("\n".join(lines))
            entry_lookup[(page_number, idx)] = entry

    return "\n\n".join(blocks), entry_lookup

# v3
_ANSWER_SYSTEM_PROMPT = """You are a technical assistant for industrial factory operators. \
Answer questions about machinery procedures based strictly on the provided context blocks.

Rules:
- Respond in {answer_language} only.
- Ground every claim in the context. If the context is insufficient, say so explicitly.
- Do NOT copy entry text verbatim. Rewrite every step in your own words using clear, \
  friendly, and professional language — as if explaining to a capable operator on the floor.
- Use active voice and imperative form ("Press the red button", not "The operator should press").
- When a context block contains a "fields" section, incorporate the relevant details naturally:
  - "Resp." → mention who performs the step (e.g. "This step is performed by the Mechanic")
  - "Material" → mention required tools/materials inline (e.g. "using clean cloths and alcohol")
  - "Tempo" → mention estimated time if it helps the operator plan (e.g. "allow ~5 minutes")
  - "Ação" / "Modo" → mention if the machine must be stopped ("machine must be stopped first")
  - Omit fields that add no practical value for the specific question asked.
- When a context block contains a "legend" section, use it to interpret any symbols mentioned \
  in the entries (e.g. eye_icon = Inspeção, wrench_icon = Intervenção).
- Structure the answer as: one-line task summary, then numbered steps.
- Immediately after any step whose context block says "has_image: yes", insert this marker \
  in EXACTLY this format: 🖼️[page {{page_number}} | entry {{entry_number}}]
- Never insert a marker for "has_image: no". Never invent page/entry numbers not in context.
- One marker per step maximum.
- Answer ONLY the scope of what was asked. If the question is about one specific step or action, \
  answer only that step. Do not expand into the full procedure unless the user explicitly asks \
  for the complete process.
- Match answer length to question scope:
  - "How do I turn off X?" → answer only the off sequence
  - "What lubricant is used?" → one line answer, no steps
  - "How do I do the full maintenance?" → full procedure is appropriate
- If in doubt, answer less. The operator can always ask for more.

At the end of every answer, add a source reference block in exactly this format:
📄 Source
- File: <source_file>
- Line: <line_number>
- Page(s): <page_number(s)>
If multiple source files or lines are referenced, list each separately.
If line_number is not present in the context, write: Line: not specified

Example output shape:
"To shut down the assembly line:
1. Turn off the sealing machine by pressing the red stop button on its control panel. 🖼️[page 4 | entry 1]
2. Stop the ring stapler by pressing both STOP buttons on the front panel. 🖼️[page 4 | entry 2]
3. Turn off the oven burners by rotating both knobs to the off position — the oven must be \
   completely cold before cleaning. 🖼️[page 4 | entry 5]

📄 Source
- File: O01.O067.1_Line5_Shutdown.xlsx
- Line: 5
- Page(s): 4"
"""


def _answer_language_for(language: str) -> str:
    """
    Maps detected query language to the (only) two allowed answer languages.
    'other' defaults to English - flagged as a business-logic default,
    confirm this is the desired fallback.
    """
    return "Portuguese" if language == "portuguese" else "English"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    reraise=True,
)
def generate_answer(
    claude_client: anthropic.AsyncAnthropic,
    query: str,
    context: str,
    language: str,
    model: str = GENERATION_MODEL,
) -> str:
    """
    Generates the final answer, constrained to English or Portuguese
    regardless of what language the retrieved context happens to be in.
    """
    answer_language = _answer_language_for(language)
    system_prompt = _ANSWER_SYSTEM_PROMPT.format(answer_language=answer_language)

    logger.info(f"Generating answer | model={model} | answer_language={answer_language}")

    resp = claude_client.messages.create(
        model=model,
        max_tokens=6000,
       system=system_prompt,          # <-- top-level
    messages=[
        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"},
    ],
    temperature=0,
)

    usage = resp.usage
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    
    logger.info(
        f"Token usage | prompt_tokens={input_tokens} |"
        f"output_tokens={output_tokens} |"
        f"total_tokens={input_tokens + output_tokens}|"
    )

    return resp.content[0].text


def extract_citations(answer: str, entry_lookup: EntryLookup) -> list[dict]:
    """
    Finds every 🖼️[page X | entry Y] marker in the answer text, in order
    of appearance, and resolves each to the real entry data.
    Returns a list of:
        {
            "page": int,
            "entry": int,
            "image_ids": list[str],
            "combined_image": str | None,
            "is_combined": bool,
            "image_description": str,
        }
    Markers that don't match a known (page, entry) pair are logged and
    skipped rather than raising - an LLM occasionally drifts on exact
    indices, and dropping one bad citation shouldn't break the response.
    """
    citations: list[dict] = []
    for match in _IMAGE_MARKER_PATTERN.finditer(answer):
        page = int(match.group(1))
        entry_number = int(match.group(2))
        entry = entry_lookup.get((page, entry_number))
        if entry is None:
            logger.warning(
                f"LLM cited unresolvable marker: page={page} entry={entry_number} "
                f"(not present in retrieved context)"
            )
            continue
        citations.append({
            "page": page,
            "entry": entry_number,
            "image_ids": entry.get("image_ids", []),
            "combined_image": entry.get("combined_image"),
            "is_combined": entry.get("is_combined", False),
            "image_description": entry.get("image_description", ""),
        })
    return citations

def extract_citations1(answer: str, entry_lookup: EntryLookup) -> list[dict]:
    citations: list[dict] = []
    for match in _IMAGE_MARKER_PATTERN.finditer(answer):
        page = int(match.group(1))
        entry_number = int(match.group(2))
        entry = entry_lookup.get((page, entry_number))
        if entry is None:
            logger.warning(
                f"LLM cited unresolvable marker: page={page} entry={entry_number} "
                f"(not present in retrieved context)"
            )
            continue

        if entry.get("is_combined"):
            image_ref = entry.get("combined_image")
        elif entry.get("image_ids"):
            image_ref = entry["image_ids"][0]
        else:
            image_ref = None

        citations.append({
            "marker": f"[page {page} | entry {entry_number}]",
            "image_ref": image_ref,
            # "image_description": entry.get("image_description", ""),
        })
    return citations

def strip_image_markers(answer: str) -> str:
    """
    Returns the answer text with raw 🖼️[page X | entry Y] markers removed -
    useful for a plain-text rendering path (e.g. TTS, logs, plain chat)
    where inline image markers aren't renderable.
    """
    return _IMAGE_MARKER_PATTERN.sub("", answer).strip()



def generate_from_retrieval(
    claude_client: anthropic.AsyncAnthropic,
    query: str,
    retrieval_response: RetrievalResponse,
    model: str = GENERATION_MODEL,
) -> dict:
    """
    Convenience wrapper: takes the RetrievalResponse produced by
    page_retrieval.retrieve(...) and returns the final answer alongside
    the language and sources used, without retrieval and generation
    living in the same function.

    Returns {"answer": str, "language": str, "results": list[dict]}
    """
    context, entry_lookup = format_context_for_llm(retrieval_response.results, retrieval_response.language)
    
    answer = generate_answer(
        claude_client, query, context, retrieval_response.language, model=model
    )
    citations = extract_citations1(answer, entry_lookup)
    log_query(question=query, language=retrieval_response.language, model=model, context=context, answer=answer)

    return {
        "answer": answer,
        "citations": citations,
        "language": retrieval_response.language,
        "results": retrieval_response.results,
    }