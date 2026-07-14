"""
Extract structured step -> image_id mapping from a document page using Claude.

Given:
  - the full page image (base64)
  - a list of already-cropped sub-images, each with an id and bbox (in the
    coordinate space of the full page image)

Claude is asked to read the page, understand the numbered steps (1, 2, 3, ...)
and the text describing each step, then decide which cropped image_id(s)
visually correspond to each step (using the bbox positions as spatial hints).

Output: a JSON object of the form
{
  "document_title": "...",
  "steps": [
    {
      "step_number": 1,
      "step_text": "...",
      "image_ids": ["page_3_0880", "page_3_ff3a", "page_3_5e18"]
    },
    ...
  ]
}

Requires: pip install anthropic
Set ANTHROPIC_API_KEY in your environment.
"""

import base64
import json
import os
from pathlib import Path
import anthropic
from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"  # current Sonnet model string

import re

def parse_llm_json(raw: str) -> list | dict:
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    cleaned = re.sub(r',(\s*[\]}])', r'\1', raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fix unescaped double-quotes inside JSON string values
        # Targets any " not already escaped and not a structural quote
        fixed = re.sub(r'(?<!\\)"(?![,\]\}\s]|:\s)(?<![{\[,:\s\n])', r'\\"', cleaned)
        return json.loads(fixed)

def encode_image(path: str) -> tuple[str, str]:
    """Return (base64_data, media_type) for an image file."""
    ext = Path(path).suffix.lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(ext, "image/png")
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return data, media_type


# ---- Tool schema: forces Claude's output into exactly the shape we want ----
STEP_MAPPING_TOOL = {
  "name": "record_step_image_mapping",
  "description": "Record document metadata, legend, and the mapping between each entry (step/point/row) and the image_id(s) that illustrate it.",
  "input_schema": {
    "type": "object",
    "properties": {
      "document_title": {
        "type": "string",
        "description": "Title/heading of the document as shown on the page."
      },
      "document_code": {
        "type": "string",
        "description": "Document code shown on page, e.g. O001.M012.1. Empty string if none visible."
      },
      "legend": {
        "type": "array",
        "description": "Symbol-to-meaning mapping if the page has a legend box. Empty array if none.",
        "items": {
          "type": "object",
          "properties": {
            "symbol": {"type": "string", "description": "Short description of the symbol, e.g. yellow_circle, green_triangle."},
            "meaning": {"type": "string", "description": "What the symbol means, as written on the page."}
          },
          "required": ["symbol", "meaning"]
        }
      },
      "entries": {
        "type": "array",
        "description": "Every numbered step, lettered point, or table row on the page, as one flat list.",
        "items": {
          "type": "object",
          "properties": {
            "entry_id": {
              "type": "string",
              "description": "The marker as shown on the page: a number (1,2,3), a letter (A,B,C), or a generated row id (row_1) for table entries."
            },
            "parent_id": {
              "type": "string",
              "description": "entry_id of the parent this entry is nested under (e.g. letter under a numbered zone). Empty string if top-level."
            },
            "entry_text": {
              "type": "string",
              "description": "Instruction text tied to this entry, as written. Empty string if the entry has no instruction text (e.g. legend-only lettered points)."
            },
            "entry_text_en": {
                "type": "string",
                "description": "English translation of entry_text. Empty string if entry_text is empty."
            },
            "fields": {
              "type": "object",
              "description": "Table column data if this entry came from a table row (column_name: value). Empty object otherwise."
            },
            "image_ids": {
              "type": "array",
              "items": {"type": "string"},
              "description": "image_id(s) from the provided list that visually correspond to this entry. Empty list if none."
            },
            "image_description": {
                "type": "string",
                "description": "One single description covering what all image_ids for this entry show collectively — not one description per image. Empty string if image_ids is empty."
            },

          },
          "required": ["entry_id", "parent_id", "entry_text", "fields", "image_ids"]
        }
      }
    },
    "required": ["document_title", "document_code", "legend", "entries"]
  }
}



def build_prompt(image_meta: list[dict]) -> str:
    """
    image_meta: list of dicts like
        {"image_id": "page_3_0880", "bbox": [x0, y0, x1, y1]}
    bbox coordinates should be in the same pixel space as the full page image.
    """
    meta_lines = "\n".join(
        f"- image_id: {m['image_id']}, bbox (x0,y0,x1,y1): {m['bbox']}"
        for m in image_meta)
    return f"""You are given the full page image of a technical/SOP document, along
with a list of sub-images that have already been cropped out of that same
page (their ids and bounding boxes in the full page's pixel coordinates are
listed below).

Sub-images available:
{meta_lines}

Your job:
1. Read the full page image carefully, including all entries on the page —
   these may be numbered steps ("1", "2", "3"...), lettered points ("A",
   "B", "C"...), or rows of a table. Also read any legend box that explains
   symbols (e.g. colored circles/triangles) and record it in `legend`.
2. For each entry, identify which of the listed image_ids show the
   image(s)/photo(s) that belong to it. Use the bbox coordinates as a
   spatial hint (an image near an entry's marker/text belongs to it)
   combined with your visual understanding of the page layout (e.g. arrows,
   proximity, section groupings, labels like "page_3_xxxx" annotations
   visible in the image itself).
3. If a lettered point sits inside/under a numbered zone (e.g. letters A, B
   grouped under step 1's photo), set that letter's `parent_id` to the
   numbered entry's `entry_id`. If an entry is top-level, leave `parent_id`
   as an empty string.
4. An entry may have zero, one, or multiple associated image_ids.
5. Every image_id in the list should be assigned to exactly one entry unless
   it is clearly not entry-specific (e.g. a logo or header image) — in that
   case simply omit it from all entries.
6. Preserve entry text as written in the document (you may lightly clean
   up whitespace/line-breaks but do not paraphrase or omit content). If an
   entry has no instruction text (e.g. a legend-only lettered point), leave
   `entry_text` as an empty string.
7. If the page is a table, use `fields` to capture each row's column data
   (column_name: value) and generate an `entry_id` like "row_1", "row_2" in
   reading order. For non-table entries, leave `fields` as an empty object.

=== IMAGE DESCRIPTION GUIDELINES ===

Write image_description as a structured paragraph covering ALL of the following:

1. MAIN SCENE (1-2 sentences)
   - What equipment, object, or environment is shown?
   - What is the overall context (e.g., control panel, workstation, tool in use)?

2. RED-MARKED REGION (2-3 sentences)
   - What specific element is circled, arrowed, or highlighted in red?
   - Describe its appearance: color, shape, position, label, or text if readable.
   - What action or state does it represent?

3. INSET / ZOOM / DETAIL VIEWS (if present)
   - Is there a magnified close-up, secondary panel, or cutaway view?
   - What does the inset emphasize that the main image might obscure?

4. VISUAL COMMUNICATION / CONNECTION TO STEP (1-2 sentences)
   - How do the marks (circles, arrows, boxes) guide the operator?
   - What ambiguity do they resolve?
   - How does what is visually shown relate to the source_text instruction for this step? Be explicit — e.g. "The red circle highlights the STOP button referenced in the step instruction."

5. TEXT IN IMAGE (if any text is visible inside the image itself, not OCR)
   - Any labels, signs, screen readouts, or button text visible in the photo.
   
IMPORTANT: All string values in your tool call must use valid JSON escaping.
Any double-quote character that appears inside a string value must be escaped as \".

Tone: Technical, precise, objective. Do not infer beyond what is visually evident.
Length: 60-100 words. Be thorough but concise.
   
Call the `record_step_image_mapping` tool with your final structured answer.
"""


def extract_steps(
    full_page_image_path: str,
    image_meta: list[dict],
    client:anthropic.Anthropic,
    api_key: str | None = None,
) -> dict:
   

    full_b64, full_media_type = encode_image(full_page_image_path)

    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": full_media_type,
                "data": full_b64,
            },
        },
        {
            "type": "text",
            "text": build_prompt(image_meta),
        },
    ]

    response = client.messages.create(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=10000,
        tools=[STEP_MAPPING_TOOL],
        tool_choice={"type": "tool", "name": "record_step_image_mapping"},
        messages=[{"role": "user", "content": content}],
    )

    for block in response.content:
      if block.type == "tool_use" and block.name == "record_step_image_mapping":
          result = block.input
          if isinstance(result.get("entries"), str):
            try:
                result["entries"] = parse_llm_json(result["entries"])
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"entries came back as malformed JSON string, parse failed: {e} | raw[:300]: {result['entries'][:300]}")
                result["entries"] = []
                result["llm_answer"] = str(result.get("entries"))
          return result

    raise RuntimeError("Model did not return the expected tool call.")


# if __name__ == "__main__":
#     # Example usage — replace with your real image path and metadata.
#     full_page_image_path = r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\outputs\e5\crops\page_3\marked\page_3_marked.png"

#     image_meta = [{'image_id': 'page_3_9a3c', 'bbox': [1026, 94, 1251, 157]}, {'image_id': 'page_3_7b9c', 'bbox': [1584, 355, 1825, 658], }, {'image_id': 'page_3_af69', 'bbox': [886, 419, 1348, 746], }, {'image_id': 'page_3_ff3a', 'bbox': [463, 620, 716, 757]}, {'image_id': 'page_3_0880', 'bbox': [247, 638, 420, 868]}, {'image_id': 'page_3_51c3', 'bbox': [1438, 766, 1942, 983]}, {'image_id': 'page_3_5e18', 'bbox': [461, 767, 716, 902] }, {'image_id': 'page_3_fd37', 'bbox': [377, 957, 618, 1261]}]

#     result = extract_steps(full_page_image_path, image_meta)
#     print(json.dumps(result, indent=2, ensure_ascii=False))