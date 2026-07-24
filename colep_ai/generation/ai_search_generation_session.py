"""
ai_search_generation.py

Generation layer for Azure AI Search retrieval.
Handles two page types:
  - SOP pages  : entries[] is populated, flowchart is {}
  - Flowchart pages: entries[] is empty, flowchart is populated

Kept deliberately separate from ai_search_retrieval.py so retrieval and
generation can be tested, versioned, and scaled independently.

Inline image markers
--------------------
The LLM emits markers like 🖼️[page 3 | entry 10] after steps that have
images. extract_citations() resolves each marker back to the real entry
(image_ids / combined_image) via the (page, entry_number) lookup built
during context formatting. Flowchart pages never produce markers.
"""

import re

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.retrieval.ai_search_retrieval import RetrievalResponse
from colep_ai.utils.query_log import log_query
from colep_ai.generation.prompts.answer_generation_prompt import _ANSWER_SYSTEM_PROMPT

logger = get_logger(__name__)

GENERATION_MODEL = settings.ANTHROPIC_MODEL

_RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)

# Matches exactly the marker format the LLM is instructed to emit.
_IMAGE_MARKER_PATTERN = re.compile(r"🖼️\[page\s+(\d+)\s*\|\s*entry\s+(\d+)\]")

EntryLookup = dict[tuple[int, int], dict]


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------

def _format_sop_page(result: dict, language: str, entry_lookup: EntryLookup) -> str:
    """
    Serializes a standard SOP page (has entries) into a context block.
    Populates entry_lookup in-place for later citation resolution.
    Returns empty string if the page has no usable entries.
    """
    page_number = result.get("page_number")
    entries = result.get("entries", [])
    if not entries:
        return ""

    legend = result.get("legend", [])
    legend_block = ""
    if legend:
        legend_lines = [
            f"  {item['symbol']}: {item['meaning']}"
            for item in legend
            if item.get("symbol") and item.get("meaning")
        ]
        if legend_lines:
            legend_block = "legend:\n" + "\n".join(legend_lines)

    blocks: list[str] = []
    for idx, entry in enumerate(entries, start=1):
        text = (
            entry.get("entry_text") if language == "pt"
            else entry.get("entry_text_en")
        ) or entry.get("entry_text_en") or entry.get("entry_text") or ""

        has_image = bool(entry.get("image_ids") or entry.get("combined_image"))

        lines = [f"[page {page_number} | entry {idx}]"]

        if result.get("document_title"):
            lines.append(f"document_title: {result['document_title']}")
        if result.get("document_code"):
            lines.append(f"document_code: {result['document_code']}")
        if result.get("source_file"):
            lines.append(f"source_file: {result['source_file']}")

        line_number = result.get("line_number")
        if line_number is not None:
            lines.append(f"line_number: {line_number}")

        # Legend injected once on the first entry of the page
        if legend_block and idx == 1:
            lines.append(legend_block)

        if text:
            lines.append(f"text: {text}")

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
        entry_lookup[(page_number, idx)] = {**entry, "_source_file": result.get("source_file", "")}

    return "\n\n".join(blocks)


def _format_flowchart_page(result: dict, language: str) -> str:
    """
    Serializes a flowchart page into a context block.
    Flowchart pages have entries=[] and a populated flowchart dict.
    Nodes are intentionally not dumped raw — the description captures
    sequence and parallelism in natural language, which the LLM handles
    better than a raw adjacency list.
    """
    flowchart = result.get("flowchart", {})
    if not flowchart:
        return ""

    page_number = result.get("page_number")
    lines = [f"[page {page_number} | flowchart]"]

    if result.get("document_title"):
        lines.append(f"document_title: {result['document_title']}")
    if result.get("document_code"):
        lines.append(f"document_code: {result['document_code']}")
    if result.get("source_file"):
        lines.append(f"source_file: {result['source_file']}")

    line_number = result.get("line_number")
    if line_number is not None:
        lines.append(f"line_number: {line_number}")

    area = flowchart.get("area")
    if area:
        lines.append(f"area: {area}")

    columns = flowchart.get("columns", [])
    if columns:
        lines.append(f"swimlane_machines: {', '.join(columns)}")

    # Shape legend — tells LLM what dashed_rectangle vs rectangle means
    shape_legend = flowchart.get("legend", {})
    if shape_legend:
        legend_lines = [f"  {k}: {v}" for k, v in shape_legend.items()]
        lines.append("shape_legend:\n" + "\n".join(legend_lines))

    # Use language-appropriate description; fall back to the other
    if language == "pt":
        description = (
            flowchart.get("flow_chart_description")
            or flowchart.get("flow_chart_description_en")
            or ""
        )
    else:
        description = (
            flowchart.get("flow_chart_description_en")
            or flowchart.get("flow_chart_description")
            or ""
        )

    if description:
        lines.append(f"process_description: {description}")

    lines.append("has_image: no")  # no per-entry image markers for flowchart pages

    return "\n".join(lines)


def format_context_for_llm(
    results: list[dict], language: str
) -> tuple[str, EntryLookup]:
    """
    Dispatches each result to the correct formatter based on whether it
    is an SOP page (has entries) or a flowchart page (has flowchart).
    Returns the assembled context string and the entry lookup for
    citation resolution.
    """
    entry_lookup: EntryLookup = {}
    blocks: list[str] = []

    for result in results:
        has_entries = bool(result.get("entries"))
        has_flowchart = bool(result.get("flowchart"))

        if has_entries:
            block = _format_sop_page(result, language, entry_lookup)
        elif has_flowchart:
            block = _format_flowchart_page(result, language)
        else:
            logger.warning(
                f"Result has neither entries nor flowchart — skipping | "
                f"source={result.get('source_file')} page={result.get('page_number')}"
            )
            continue

        if block:
            blocks.append(block)

    return "\n\n".join(blocks), entry_lookup

# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _answer_language_for(language: str) -> str:
    return "Portuguese" if language == "pt" else "English"


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
    history_messages: list[dict] | None = None,
) -> str:
    """
    history_messages: pre-built Claude messages list from
    conversation.history.build_history_messages(). Contains the
    running summary (as a synthetic exchange) + last N verbatim turns.
    If None or empty, behaves exactly as before (stateless).
    """
    answer_language = _answer_language_for(language)
    system_prompt = _ANSWER_SYSTEM_PROMPT.format(answer_language=answer_language)

    logger.info(
        f"Generating answer | model={model} | answer_language={answer_language} "
        f"| history_turns={len(history_messages) if history_messages else 0}"
    )

    # Build messages: history (summary + prior turns) + current query
    # History messages already have correct role alternation.
    # Current query is always the final user message.
    messages = list(history_messages) if history_messages else []
    messages.append({
        "role": "user",
        "content": f"Context:\n{context}\n\nQuestion: {query}",
    })

    resp = claude_client.messages.create(
        model=model,
        max_tokens=6000,
        system=system_prompt,
        messages=messages,
        temperature=0,
    )

    usage = resp.usage
    logger.info(
        f"Token usage | input={usage.input_tokens} | output={usage.output_tokens} "
        f"| total={usage.input_tokens + usage.output_tokens}"
    )

    return resp.content[0].text


# ---------------------------------------------------------------------------
# Citation extraction
# ---------------------------------------------------------------------------

def extract_citations(answer: str, entry_lookup: EntryLookup) -> list[dict]:
    """
    Finds every 🖼️[page X | entry Y] marker in the answer, resolves each
    to real entry data. Unresolvable markers are logged and skipped.
    Flowchart pages never emit markers, so no flowchart-specific logic needed here.
    """
    citations: list[dict] = []

    for match in _IMAGE_MARKER_PATTERN.finditer(answer):
        page = int(match.group(1))
        entry_number = int(match.group(2))
        entry = entry_lookup.get((page, entry_number))

        if entry is None:
            logger.warning(
                f"LLM cited unresolvable marker: page={page} entry={entry_number}"
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
        "image_description": entry.get("image_description", ""),
        "source_file": entry.get("_source_file", ""),
        "page_number": page,
    })

    return citations


def strip_image_markers(answer: str) -> str:
    """Removes 🖼️ markers — for plain-text rendering paths (TTS, logs)."""
    return _IMAGE_MARKER_PATTERN.sub("", answer).strip()


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def generate_from_retrieval(
    claude_client: anthropic.Anthropic,
    query: str,
    retrieval_response: RetrievalResponse,
    model: str = GENERATION_MODEL,
    history_messages: list[dict] | None = None,
) -> dict:
    """
    Takes the RetrievalResponse from ai_search_retrieval.retrieve() and
    returns the final answer with citations and source results.

    Returns:
        {
            "answer": str,
            "citations": list[dict],
            "language": str,
            "results": list[dict],
        }
    """
    context, entry_lookup = format_context_for_llm(
        retrieval_response.results, retrieval_response.language
    )

    answer = generate_answer(
        claude_client,
        query,
        context,
        retrieval_response.language,
        model=model,
        history_messages=history_messages,
    )

    logger.info(f"RAW ANSWER:\n{repr(answer)}")
    strip_image_markers(answer)

    citations = extract_citations(answer, entry_lookup)

    log_query(
        question=query,
        language=retrieval_response.language,
        model=model,
        context=context,
        answer=answer,
    )

    return {
        "answer": answer,
        "citations": citations,
        "language": retrieval_response.language,
        "results": retrieval_response.results,
    }