# Flowchart extractor · PY
"""
flowchart_extractor.py
Extract structured JSON from a Colep production flowchart image using Claude Sonnet.
 
Usage:
    python flowchart_extractor.py --image page_0.png
    python flowchart_extractor.py --image page_0.png --output result.json
"""
 
import argparse
import base64
import json
import sys
from pathlib import Path
from colep_ai.generation.claude_client import get_claude_client
import anthropic
 
# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
 
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MEDIA_TYPE_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
 
SYSTEM_PROMPT = """You are an expert at analyzing Portuguese industrial production flowcharts (Fluxogramas Produtivos) from manufacturing facilities.
You extract complete, accurate structured data from flowchart images.
You return ONLY valid JSON — no markdown fences, no explanation, no preamble."""
 
EXTRACTION_PROMPT = """Analyze this Portuguese industrial production flowchart image and extract its complete structure.
 
## What to extract
 
For each node in the flowchart, identify:
- `step_id`: sequential integer, numbered top-to-bottom, left-to-right starting from 1
- `label`: exact text as written (keep Portuguese, preserve accents)
- `shape`: one of ["rectangle", "diamond", "oval", "parallelogram", "dashed_rectangle", "circle"]
- `column`: the swim-lane/column header this node belongs to (null if not in a swim lane)
- `connections`: list of step_ids this node connects TO (following arrow direction)
- `condition`: if shape is "diamond" (decision node), the branch labels on outgoing arrows keyed by target step_id; otherwise null
 
## Output format
 
Return ONLY valid JSON matching this exact schema:
 
{
  "document_title": "<exact title from header>",
  "document_code": "<document code e.g. O01.F001.2>",
  "flowchart": {
    "area": "<area/line e.g. FOOD/Linha 20 e 22>",
    "columns": ["<swim-lane column 1>", "<swim-lane column 2>"],
    "nodes": [
      {
        "step_id": 1,
        "label": "<exact text>",
        "shape": "oval",
        "column": null,
        "connections": [2],
        "condition": null
      }
    ],
    "legend": {
      "<shape description>": "<meaning>"
    },
    "flow_chart_description": "<natural language narrative of the complete process from start to end, in Portuguese, 3-6 sentences capturing all branches and loops in english >"
  }
}
 
## Rules
- Number nodes top-to-bottom as the primary direction
- If arrows loop back (rework loops), capture them — connections can reference lower step_ids
- Do not invent nodes — only extract what is visually present
- If text is partially obscured, transcribe what is visible and append [?]
- Capture the legend/key section if present (shape types and their meanings)
- The `flow_chart` field is a human-readable narrative of the end-to-end process — this is critical"""
 
 
# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
 
def load_image_b64(image_path: Path | str) -> tuple[str, str]:
    """Return (base64_data, media_type)."""
    image_path = Path(image_path)
    ext = image_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported image type: {ext}. Supported: {SUPPORTED_EXTENSIONS}")
    media_type = MEDIA_TYPE_MAP[ext]
    data = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")
    return data, media_type
 
 
def extract_flowchart(image_path: Path | str, client: anthropic.Anthropic) -> dict:
    """Send image to Sonnet and return parsed JSON."""
    b64_data, media_type = load_image_b64(image_path)
 
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT,
                    },
                ],
            }
        ],
    )
 
    raw_text = response.content[0].text.strip()
 
    # Strip accidental markdown fences if model slips
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()
 
    return json.loads(raw_text)
 
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)
from pathlib import Path

if __name__ == "__main__":

    image_folder=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\outputs\O01_F003_2_Fluxograma_Fluxograma_produtivo_L_23_Montagem\pdf_pages_images"

    DOCS_DIR = Path(image_folder)
    images = list(DOCS_DIR.glob("*.png"))
    client =get_claude_client()
    for i in images[:1]:
        try:
            logger.info(f"Processing: {i.name}")            
            result = extract_flowchart(i, client)
            path=Path(f"{image_folder}/{i.stem}.json")
            with open(path,"w",encoding="utf-8")as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print("saved")

        except Exception as e:
            logger.warning(f"❌ Failed: {i.name}")
            logger.error(e)