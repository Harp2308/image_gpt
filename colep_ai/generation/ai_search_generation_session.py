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
# from colep_ai.utils.query_log import log_query
from colep_ai.generation.prompts.answer_generation_prompt import _ANSWER_SYSTEM_PROMPT

logger = get_logger(__name__)

GENERATION_MODEL = settings.ANTHROPIC_MODEL

# Correct SDK exceptions for transient network failures.
# RateLimitError is intentionally excluded — handled separately below,
# not retried blindly (window is 60s, blind retries won't help).
_RETRYABLE_EXCEPTIONS = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)
# ---------------------------------------------------------------------------
# helpers for image and pdf marker resolution
# ---------------------------------------------------------------------------

# Format: 🖼️[doc D | page P | entry E]
_IMAGE_MARKER_PATTERN = re.compile(
    r"🖼️\[doc\s+(\d+)\s*\|\s*page\s+(\d+)\s*\|\s*entry\s+(\d+)\]"
)
_MAP_MARKER_PATTERN = re.compile(r"🖼️\[page\s+(\d+)\s*\|\s*map\]")
EntryLookup = dict[tuple[int, int, int], dict]

def _build_pdf_url(folder_name: str, source_file: str) -> str:
    return f"/blob/view/{folder_name}/{source_file}/pdf/{source_file}.pdf"


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------

def _format_sop_page(
    result: dict, language: str, entry_lookup: EntryLookup, doc_index: int
)-> str:
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

        lines = [f"[doc {doc_index} | page {page_number} | entry {idx}]"]


        if result.get("document_title"):
            lines.append(f"document_title: {result['document_title']}")
        if result.get("document_code"):
            lines.append(f"document_code: {result['document_code']}")
        if result.get("source_file"):
            lines.append(f"source_file: {result['source_file']}")
        if result.get("periodicity"):
            lines.append(f"periodicity: {result['periodicity']}")
        if result.get("sheet_name"):
            lines.append(f"sheet_name: {result['sheet_name']}")

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
        entry_lookup[(doc_index, page_number, idx)] = {
            **entry,
            "_source_file": result.get("source_file", ""),
            "_folder_name": result.get("folder_name", ""),
        }
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

def _format_map_page(result: dict, language: str) -> str:
    map_data = result.get("map", {})
    if not map_data:
        return ""

    page_number = result.get("page_number")
    lines = [f"[page {page_number} | map]"]

    if result.get("document_title"):
        lines.append(f"document_title: {result['document_title']}")
    if result.get("document_code"):
        lines.append(f"document_code: {result['document_code']}")
    if result.get("source_file"):
        lines.append(f"source_file: {result['source_file']}")
    if result.get("line_number"):
        lines.append(f"line_number: {result['line_number']}")

    if map_data.get("map_area"):
        lines.append(f"map_area: {map_data['map_area']}")

    desc_key = "map_description" if language == "pt" else "map_description_en"
    description = map_data.get(desc_key) or map_data.get("map_description", "")
    if description:
        lines.append(f"map_description: {description}")

    map_image = map_data.get("combined_image", "")
    if map_image:
        lines.append("has_image: yes")
        lines.append(f"map_image: {map_image}")
    else:
        lines.append("has_image: no")
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
    map_lookup: dict[int, dict] = {}
    blocks: list[str] = []
    doc_index = 0  # incremented only for SOP pages that produce a block

    for result in results:
        has_entries = bool(result.get("entries"))
        has_flowchart = bool(result.get("flowchart"))
        has_map = bool(result.get("map"))
        if has_entries:
            doc_index += 1
            block = _format_sop_page(result, language, entry_lookup, doc_index)
        elif has_flowchart:
            block = _format_flowchart_page(result, language)
        elif has_map:
            block = _format_map_page(result, language)
            if block:
                map_lookup[result.get("page_number")] = {
                    "map": result.get("map", {}),
                    "_source_file": result.get("source_file", ""),
                    "_folder_name": result.get("folder_name", ""),
                }
        else:
            logger.warning(
                f"Result has neither entries nor flowchart — skipping | "
                f"source={result.get('source_file')} page={result.get('page_number')}"
            )
            continue

        if block:
            blocks.append(block)

    return "\n\n".join(blocks), entry_lookup, map_lookup

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
async def generate_answer(
    claude_client: anthropic.AsyncAnthropic,
    query: str,
    context: str,
    language: str,
    model: str = GENERATION_MODEL,
    history_messages: list[dict] | None = None,
) -> str:
    """
    Calls Claude async. Retries on connection/timeout errors only.
    RateLimitError is caught here and re-raised as-is — the route
    handler catches it and returns a clean 503.
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

    try:
        resp = await claude_client.messages.create(
            model=model,
            max_tokens=6000,
            system=system_prompt,
            messages=messages,
            temperature=0,
        )
    except anthropic.RateLimitError as exc:
        # Do not retry — window is 60s, retrying immediately won't help.
        # Re-raise so the route handler can surface a clean 503.
        logger.warning(f"Anthropic rate limit hit | {exc}")
        raise

    usage = resp.usage
    logger.info(
        f"Token usage | input={usage.input_tokens} | output={usage.output_tokens} "
        f"| total={usage.input_tokens + usage.output_tokens}"
    )

    return resp.content[0].text, {"input": usage.input_tokens, "output": usage.output_tokens}


# ---------------------------------------------------------------------------
# Citation extraction
# ---------------------------------------------------------------------------


def extract_citations(answer: str, entry_lookup: EntryLookup, map_lookup: dict | None = None, results: list[dict] | None = None) -> list[dict]:
    """
    Finds every 🖼️[doc D | page P | entry E] marker in the answer, resolves
    each to real entry data via the (doc_index, page, entry) lookup.
    Unresolvable markers are logged and skipped.
    Flowchart pages never emit markers, so no flowchart-specific logic needed here.
    """
    citations: list[dict] = []

    for match in _IMAGE_MARKER_PATTERN.finditer(answer):
        doc_index = int(match.group(1))
        page = int(match.group(2))
        entry_number = int(match.group(3))
        entry = entry_lookup.get((doc_index, page, entry_number))

        if entry is None:
            logger.warning(
                f"LLM cited unresolvable marker: "
                f"doc={doc_index} page={page} entry={entry_number}"
            )
            continue

        if entry.get("is_combined"):
            image_ref = entry.get("combined_image")
        elif entry.get("image_ids"):
            image_ref = entry["image_ids"][0]
        else:
            image_ref = None

        citations.append({
            "marker": f"[doc {doc_index} | page {page} | entry {entry_number}]",
            "image_ref": image_ref,
            "image_description": entry.get("image_description", ""),
            "source_file": entry.get("_source_file", ""),
            "folder_name": entry.get("_folder_name", ""), 
            "page_number": page,
        })

    for match in _MAP_MARKER_PATTERN.finditer(answer):
        page = int(match.group(1))
        # find the map result for this page
        map_result = (map_lookup or {}).get(page)
        if map_result:
            citations.append({
                "marker": f"[page {page} | map]",
                "image_ref": map_result.get("map", {}).get("combined_image"),
                "image_description": map_result.get("map", {}).get("map_image_description", ""),
                "source_file": map_result.get("_source_file", ""),
                "folder_name": map_result.get("_folder_name", ""),
                "page_number": page,
            })
     # Fallback: no image markers — build source-level citations from results
    
    if not citations and results:
        seen: set[str] = set()
        for r in results:
            sf = r.get("source_file", "")
            fn = r.get("folder_name", "")
            if not sf or sf in seen:
                continue
            seen.add(sf)
            citations.append({
                "marker": None,
                "image_ref": None,
                "image_description": "",
                "source_file": sf,
                "folder_name": fn,
                "page_number": r.get("page_number"),
                "pdf_url": _build_pdf_url(fn, sf) if fn and sf else None,
            })

    return citations

def strip_image_markers(answer: str) -> str:
    """Removes 🖼️[doc D | page P | entry E] markers — for plain-text rendering paths (TTS, logs)."""
    return _IMAGE_MARKER_PATTERN.sub("", answer).strip()


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

async def generate_from_retrieval(
    claude_client: anthropic.AsyncAnthropic,
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
    context, entry_lookup, map_lookup = format_context_for_llm(
        retrieval_response.results, retrieval_response.language
    )

    answer, generation_tokens = await generate_answer(
        claude_client,
        query,
        context,
        retrieval_response.language,
        model=model,
        history_messages=history_messages,
    )

    logger.info(f"RAW ANSWER:\n{repr(answer)}")
    strip_image_markers(answer)

    citations = extract_citations(answer, entry_lookup, map_lookup, results=retrieval_response.results)

    # log_query(
    #     question=query,
    #     language=retrieval_response.language,
    #     model=model,
    #     context=context,
    #     answer=answer,
    # )

    return {
        "answer": answer,
        "citations": citations,
        "language": retrieval_response.language,
        "results": retrieval_response.results,
        "context": context, 
        "generation_tokens": generation_tokens,
    }