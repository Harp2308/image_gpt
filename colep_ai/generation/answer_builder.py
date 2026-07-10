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

def format_context_for_llm(results: list[dict], language: str) -> str:
    """
    Formats fused retrieval results into an LLM-ready context string,and
    returns an (page_number, entry_number) -> entry lookup so any image
    markers the LLM emits can later be resolved to real image data.

    Per entry:
      [page {page_number} | entry {n}]
      document_title: ...
      document_code: ...        (omitted if empty)
      text: <entry_text_en unless language == 'portuguese', else entry_text>
      image_description: ...    (omitted if absent)
      has_image: yes/no         (tells the LLM whether a marker is valid here)

    Text-field selection rule: Portuguese queries get the original
    entry_text (native language, most faithful); everything else
    (english / other) gets entry_text_en, since the answer is only ever
    produced in English or Portuguese and English is the fallback.
    """
    use_native_pt_text = language == "portuguese"
    blocks: list[str] = []
    entry_lookup: EntryLookup = {}


    for result in results:
        payload = result.get("payload", {})
        page_number = payload.get("page_number")
        entries = payload.get("entries", [])

        for idx, entry in enumerate(entries, start=1):
            text = entry.get("entry_text") if use_native_pt_text else entry.get("entry_text_en")
            # Fallback if the preferred field is empty for this entry
            # (e.g. entries that are table-only rows with no free text).
            if not text:
                text = entry.get("entry_text_en") or entry.get("entry_text") or ""
            has_image = bool(entry.get("image_ids") or entry.get("combined_image"))

            lines = [f"[page {page_number} | entry {idx}]"]

            if payload.get("document_title"):
                lines.append(f"document_title: {payload['document_title']}")
            if payload.get("document_code"):
                lines.append(f"document_code: {payload['document_code']}")
            if text:
                lines.append(f"text: {text}")
            if entry.get("image_description"):
                lines.append(f"image_description: {entry['image_description']}")
                lines.append(f"has_image: {'yes' if has_image else 'no'}")

            blocks.append("\n".join(lines))
            entry_lookup[(page_number, idx)] = entry

    return "\n\n".join(blocks), entry_lookup


_ANSWER_SYSTEM_PROMPT = """You are a technical assistant answering questions about industrial \
machinery based strictly on the provided context.

Rules:
- Respond in {answer_language} only, regardless of the language used in the context blocks.
- Base your answer only on the provided context. If the context does not contain enough \
information to answer, say so explicitly rather than guessing.
- Structure the answer as: a one-line main task summary, then the steps needed to \
accomplish it (as a numbered or naturally flowing list, whichever reads better).
- Immediately after any step that has a corresponding image in the context \
(has_image: yes), insert an inline marker in EXACTLY this format, with no \
extra spaces or punctuation changes:
  🖼️[page {{page_number}} | entry {{entry_number}}]
  Use the exact page and entry numbers shown in that context block's \
  "[page X | entry Y]" header.
- Only insert a marker for an entry whose context block says "has_image: yes". \
Never invent a marker for an entry that says "has_image: no", and never invent \
page/entry numbers that are not present in the context.
- Do not add more than one marker per step, and only add a marker when the \
referenced image is genuinely what illustrates that step.
Example of the expected output shape:
"To turn on the furnace conveyor: Locate the control panel 🖼️[page 3 | entry 9] \
and press the green \"Arranque\" button to start the conveyor. Next, turn on the \
furnace burners 🖼️[page 3 | entry 10] by rotating both knobs to the \"Ligado\" \
position." """


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
    claude_client: anthropic.Anthropic,
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
    claude_client: anthropic.Anthropic,
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

    return {
        "answer": answer,
        "citations": citations,
        "language": retrieval_response.language,
        "results": retrieval_response.results,
    }



