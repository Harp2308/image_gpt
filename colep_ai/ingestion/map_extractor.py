"""
map_resolver.py
Extract structured zone -> image_id mapping from a Colep safety/location map page.

Document type: "Mapa de Localização de Sistema de Segurança" and similar spatial
map documents where equipment zones (numbered 1,2,3...) and general zones
(lettered A,B,C...) are placed on a schematic floor plan.

Given:
  - the full marked page image (base64)
  - a list of already-cropped sub-images with id and bbox in page pixel coords

Output schema:
{
  "document_title": "...",
  "document_code": "...",
  "map_area": "...",
  "symbol_legend": [...],
  "zones": [...],
  "map_description": "...",
  "map_description_en": "..."
}
"""

import base64
import json
import re
from pathlib import Path

import anthropic
from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.llms.llm_retry import with_anthropic_retry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers (identical to image_group_resolver pattern)
# ---------------------------------------------------------------------------

def parse_llm_json(raw: str) -> list | dict:
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    cleaned = re.sub(r',(\s*[\]}])', r'\1', raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        fixed = re.sub(r'(?<!\\)"(?![,\]\}\s]|:\s)(?<![{\[,:\s\n])', r'\\"', cleaned)
        return json.loads(fixed)


def encode_image(path: str) -> tuple[str, str]:
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


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

MAP_ZONE_TOOL = {
    "name": "record_map_zone_mapping",
    "description": (
        "Record all structured data from a Colep safety/location map page: "
        "document metadata, symbol legend, equipment zones, general zones, "
        "and the image_ids spatially associated with each zone."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "document_title": {
                "type": "string",
                "description": "Exact title from the page header, e.g. 'Mapa de Localização de Sistema de Segurança'."
            },
            "document_code": {
                "type": "string",
                "description": "Document code visible on the page, e.g. 'O01.T022.1'. Empty string if not visible."
            },
            "map_area": {
                "type": "string",
                "description": (
                    "The production area, line, or machine this map covers, "
                    "as written on the page (e.g. 'Linha 93 FOOD', 'L13'). "
                    "Empty string if not stated."
                )
            },
            "symbol_legend": {
                "type": "array",
                "description": (
                    "Every symbol defined in the legend box on the page. "
                    "Empty array if no legend is present."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol_key": {
                            "type": "string",
                            "description": (
                                "Short machine-readable key for the symbol, "
                                "e.g. 'emergency_button', 'protection_gate', 'general_power', "
                                "'you_are_here'. Use snake_case, English."
                            )
                        },
                        "symbol_label_pt": {
                            "type": "string",
                            "description": "Exact Portuguese label as written in the legend, e.g. 'Botão de emergência'."
                        },
                        "symbol_label_en": {
                            "type": "string",
                            "description": "English translation of symbol_label_pt."
                        },
                        "visual_description": {
                            "type": "string",
                            "description": (
                                "What the icon looks like: color, shape, and any distinguishing marks. "
                                "E.g. 'red mushroom-head button icon', 'green cross inside a square'."
                            )
                        },
                        "image_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "image_id(s) from the provided list whose bbox falls inside the legend box "
                                "and visually shows this symbol's icon. Empty list if none."
                            )
                        }
                    },
                    "required": ["symbol_key", "symbol_label_pt", "symbol_label_en", "visual_description", "image_ids"]
                }
            },
            "zones": {
                "type": "array",
                "description": (
                    "Every equipment zone (numbered) and general zone (lettered) visible on the map. "
                    "One entry per distinct zone marker."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "zone_id": {
                            "type": "string",
                            "description": (
                                "The marker as shown on the map: a number ('1','2','6') for equipment zones "
                                "or a letter ('A','B') for general zones."
                            )
                        },
                        "zone_type": {
                            "type": "string",
                            "enum": ["equipment", "general"],
                            "description": "'equipment' for numbered zones, 'general' for lettered zones."
                        },
                        "zone_name_pt": {
                            "type": "string",
                            "description": (
                                "Portuguese name from the legend list on the page, "
                                "e.g. 'Mesa de Balote de Folha'. "
                                "Empty string if this zone_id has no corresponding legend entry."
                            )
                        },
                        "zone_name_en": {
                            "type": "string",
                            "description": "English translation of zone_name_pt. Empty string if zone_name_pt is empty."
                        },
                        "spatial_position": {
                            "type": "string",
                            "description": (
                                "Rough spatial description of where this zone sits on the map schematic, "
                                "relative to other zones or map edges. "
                                "E.g. 'top-left corner', 'center of line, between zone 3 and zone 5', "
                                "'bottom-right, near the output conveyor'. "
                                "Be concise — 1 short sentence."
                            )
                        },
                        "safety_devices": {
                            "type": "array",
                            "description": (
                                "Safety device icons visually placed ON or immediately adjacent to this zone "
                                "on the schematic. Use the symbol_key values from symbol_legend."
                            ),
                            "items": {"type": "string"}
                        },
                    },
                    "required": [
                        "zone_id", "zone_type", "zone_name_pt", "zone_name_en",
                        "spatial_position", "safety_devices"
                    ]
                }
            },
            "map_image_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "image_ids from the provided list that show the map schematic itself — "
                    "exclude any image_id already assigned to a symbol_legend entry (legend box crops), "
                    "and exclude scheduling/sign-off grid images (dense checkbox matrices). "
                    "Do not distribute image_ids per zone."
                )
            },
            "map_image_description": {
                "type": "string",
                "description": (
                    "Structured prose for RAG retrieval covering in order: "
                    "(1) line identity and flow direction, "
                    "(2) every zone by id, name, and spatial position, "
                    "(3) per-zone safety device inventory with type and count, "
                    "(4) zones with no devices and highest-density zone, "
                    "(5) general zones and what they encompass spatially. "
                    "100-180 words. No padding. Empty string if map_image_ids is empty."
                )
            },
            "map_description": {
                "type": "string",
                "description": (
                    "Natural language narrative of the complete safety map in Portuguese. "
                    "3-5 sentences covering: what production line/area the map represents, "
                    "the general layout of equipment zones from input to output, "
                    "and the distribution of safety devices (emergency buttons, protection gates) across the line."
                )
            },
            "map_description_en": {
                "type": "string",
                "description": "English translation of map_description."
            }
        },
        "required": [
            "document_title", "document_code", "map_area",
            "symbol_legend", "zones", "map_image_ids", "map_image_description",
            "map_description", "map_description_en"
        ]
    }
}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_map_prompt(image_meta: list[dict]) -> str:
    meta_lines = "\n".join(
        f"- image_id: {m['image_id']}, bbox (x0,y0,x1,y1): {m['bbox']}"
        for m in image_meta
    )
    return f"""You are given the full marked page image of a Colep industrial safety/location map
("Mapa de Localização de Sistema de Segurança" or similar), along with a list of
sub-images already cropped from that same page. Their ids and bounding boxes in the
full page's pixel coordinates are listed below.

Sub-images available:
{meta_lines}

=== YOUR TASK ===

This is a SPATIAL MAP document, not a sequential SOP. It shows a schematic floor plan
of a production line with:
  - Numbered equipment zones (1, 2, 3...) each labeled with a machine name in the legend
  - Lettered general zones (A, B, C...) for overall machine areas
  - Safety device icons placed directly on the schematic at their physical location:
      * Emergency stop buttons (typically red mushroom-head icon)
      * Protection gates/barriers (typically red horizontal bar icon)
      * General power switches (typically green cross/plus icon)
      * "You are here" marker (if present)
  - A legend box (usually top-right or right side) defining the icons and listing zone names

=== EXTRACTION RULES ===

1. LEGEND: Read the legend box completely.
   - Extract every symbol definition with its exact Portuguese label.
   - Extract every numbered/lettered zone with its exact Portuguese name.

2. ZONES: For each zone marker visible on the schematic (number or letter):
   - Match it to its name from the legend list.
   - Identify which safety device icons (by symbol_key) are visually placed
     ON or immediately next to that zone on the schematic.
   - Describe its spatial position relative to the overall map layout.

3. IMAGE ASSIGNMENT — two buckets, mutually exclusive:

   BUCKET A — symbol_legend image_ids:
   - Look at the page image and locate the legend box (the bordered panel that defines
     the safety icons with labels like "Botão de emergência", "Porta de Proteção", etc.).
   - Any image_id whose bbox visually falls inside that legend box belongs here.
   - For each such image_id, identify which symbol icon it shows and assign it to that
     symbol_legend entry's image_ids field.

   BUCKET B — map_image_ids (flat list):
   - Every image_id NOT in Bucket A and NOT a scheduling/sign-off grid (dense checkbox
     matrices with day abbreviations like Sg, Tr, Q1, Sá) or header/logo goes here.
   - Do NOT distribute map_image_ids per zone.

   Every image_id must end up in exactly one bucket or be omitted entirely (admin/header only).

4. SPATIAL POSITION: For each zone, give a concise 1-sentence description
   of where it sits on the schematic (e.g. "far left of the line, at the
   sheet input end", "center of the line adjacent to zone 3").
   Base this on what you visually see in the map, not on the legend position.

5. MAP DESCRIPTION: Produce two fields:
   map_description (Portuguese) and map_description_en (English translation).
   
   CRITICAL: Do not infer, assume, or complete anything not explicitly 
   visible on the map. If a detail is not visually present, omit it entirely.

   Cover only these points, and only if explicitly shown:

   a) Map identity: state the line name or number and document type 
      if visible on the map. If not visible, omit.

   b) Equipment sequence: list zones in flow order as 
      "Zone 1 (name) → Zone 2 (name) → ..."
      only if flow direction is explicitly indicated by arrows or labels.
      If flow direction is not marked, state zones as listed in legend 
      without implying sequence.

   c) Control hierarchy: if general zones (A, B etc.) are present, 
      name them and state their labels exactly as written.
      Do not state which numbered zones they govern unless 
      this is explicitly shown on the map.

   d) Emergency stop locations: for each emergency stop icon visible, 
      state the zone number it is physically placed in or immediately 
      adjacent to. Use "adjacent to" not "protects" — the map shows 
      location only, not functional coverage.
      Never use the word "distributed". Name each zone explicitly.

   e) Protection gate locations: list only zones where a protection 
      gate icon is explicitly visible. If a zone has both an emergency 
      stop and a protection gate, state both. Do not infer coverage.

   f) Tag density: if mqp tags are visible, state which zone has the 
      highest count and give the number. If no mqp tags are visible 
      on this map, omit this point entirely. Do not invent tag presence.

   Do not exceed 8 sentences total.
   Forbidden words: "distributed", "throughout", "various", 
   "several", "covers", "protects", "governs".
   Every safety device mentioned must reference a specific zone ID.
   Cross-references to MQP documents are captured in the zones array,
   not in this description.

6. MAP IMAGE DESCRIPTION (map_image_description):
   Write structured prose in English covering IN ORDER:
   (a) Line identity and flow direction.
   (b) Every zone: id, name, spatial position.
   (c) Per-zone safety device inventory — type and count per zone,
       e.g. "Zone 3 (Prensa): 2x emergency_button, 1x protection_gate."
   (d) Zones with no safety devices. Highest-density zone.
   (e) General zones (lettered) and what they encompass spatially.
   100-180 words. No padding.

7. JSON ESCAPING: All string values must use valid JSON escaping.
   Any literal double-quote inside a string must be escaped as \\".

Call the `record_map_zone_mapping` tool with your complete structured answer.
"""


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_map_zones(
    full_page_image_path: str,
    image_meta: list[dict],
    client: anthropic.Anthropic,
) -> dict:
    """
    Extract zone->image_id mapping from a safety/location map page.

    Args:
        full_page_image_path: Path to the marked full-page image.
        image_meta: List of {"image_id": str, "bbox": [x0, y0, x1, y1]}.
        client: Sync anthropic.Anthropic client (Celery task context).

    Returns:
        Parsed dict matching the MAP_ZONE_TOOL schema.
    """
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
            "text": build_map_prompt(image_meta),
        },
    ]
     
    response = with_anthropic_retry(
        lambda: client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=8000,
            tools=[MAP_ZONE_TOOL],
            tool_choice={"type": "tool", "name": "record_map_zone_mapping"},
            messages=[{"role": "user", "content": content}],
        ),
        caller_label="extract_map_zones",

    )

    usage = response.usage
    logger.info(
        f"map_resolver | input_tokens={usage.input_tokens} | "
        f"output_tokens={usage.output_tokens} | "
        f"total={usage.input_tokens + usage.output_tokens}"
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "record_map_zone_mapping":
            result = block.input
            # Safety net: zones came back as a JSON string instead of array
            if isinstance(result.get("zones"), str):
                try:
                    result["zones"] = parse_llm_json(result["zones"])
                except (json.JSONDecodeError, ValueError) as e:
                    logger.error(
                        f"zones came back as malformed JSON string, parse failed: {e} | "
                        f"raw[:300]: {result['zones'][:300]}"
                    )
                    result["zones"] = []
            return result

    raise RuntimeError("Model did not return the expected tool call: record_map_zone_mapping")
