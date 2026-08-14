"""
page_classifier.py
Vision-based page classifier for the Colep ingestion pipeline.

Given a page image, returns one of four route labels:
  - "flowchart"  → Fluxograma Produtivo pages
  - "map"        → Mapa de Localização de Sistema de Segurança pages
  - "sop"        → All other content pages (OPL, tables with photos, parameter sheets, cover pages)
  - "skip"       → Empty forms, blank checklists, sign-off grids — no extractable content

Usage:
    label = classify_page(page_image_path, client)
    label = classify_page_azure(page_image_path, client)

"""

import base64
from pathlib import Path
from typing import Literal

import anthropic
from openai import AzureOpenAI

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.llms.llm_retry import with_openai_retry

logger = get_logger(__name__)

PageRoute = Literal["flowchart", "map", "sop", "skip"]

MODEL =  settings.ANTHROPIC_MODEL # Fast + cheap — classification only
OPENAI_MODEL = settings.OPENAI_MODEL

SYSTEM_PROMPT = """You are a document page classifier for Colep Packaging industrial documents.
You look at a page image and return exactly one classification label.
You return ONLY the label — no explanation, no punctuation, no extra text."""

CLASSIFICATION_PROMPT = """Classify this document page into exactly one of these four categories:

flowchart
  - A process flow diagram (Fluxograma Produtivo) with shapes connected by arrows
  - Contains ovals, rectangles, diamonds, dashed boxes connected in a flow
  - Has swim-lane columns (Prensa, Forno, etc.) on the side

map
  - A safety/location map (Mapa de Localização de Sistema de Segurança)
  - Shows a schematic floor plan of a production line or machine layout
  - Has numbered equipment zones (1, 2, 3...) and/or lettered general zones (A, B...)
  - Has a legend with safety device icons (emergency buttons, protection gates)

skip
  - A completely empty form, blank checklist, or sign-off tracking sheet
  - Contains only a grid/table structure with empty cells for filling in dates or signatures
  - Has no instructional content, no photos, no process steps — purely administrative
  - Examples: weekly cleaning logs, blank maintenance checklists, sign-off grids

sop
  - Everything else: OPL step-by-step pages, parameter tables with photos,
    cover pages, maintenance tables with images, inspection plans, any page
    with actual instructional or technical content

Reply with exactly one word: flowchart, map, skip, or sop"""


def _encode_image(path: str) -> tuple[str, str]:
    ext = Path(path).suffix.lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(ext, "image/png")
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("utf-8")
    return data, media_type


def classify_page(
    page_image_path: str,
    client: anthropic.Anthropic,
) -> PageRoute:
    """
    Classify a page image into a pipeline route.

    Args:
        page_image_path: Path to the rendered page PNG.
        client:          Sync anthropic.Anthropic client (Celery task context).

    Returns:
        One of: "flowchart", "map", "sop", "skip"
    """
    b64, media_type = _encode_image(page_image_path)

    response = client.messages.create(
        model=MODEL,
        max_tokens=10,
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
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": CLASSIFICATION_PROMPT,
                    },
                ],
            }
        ],
    )

    raw = response.content[0].text.strip().lower()

    valid: set[PageRoute] = {"flowchart", "map", "sop", "skip"}
    if raw not in valid:
        logger.warning(
            f"classify_page got unexpected label '{raw}' for {page_image_path} — defaulting to 'sop'"
        )
        return "sop"

    logger.info(f"classify_page | label={raw} | image={Path(page_image_path).name}")
    return raw  # type: ignore[return-value]

def classify_page_azure(
    page_image_path: str,
    client: AzureOpenAI,
) -> PageRoute:
    """
    Classify a page image into a pipeline route using Azure OpenAI.
 
    Args:
        page_image_path: Path to the rendered page PNG.
        client:          AzureOpenAI client.
 
    Returns:
        One of: "flowchart", "map", "sop", "skip"
 
    Raises:
        openai.RateLimitError after 3 retries with exponential backoff (60s/120s/240s).
        Any other OpenAI exception immediately.
    """
    b64, media_type = _encode_image(page_image_path)
 
    response = with_openai_retry(
        lambda: client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{b64}",
                            },
                        },
                        {
                            "type": "text",
                            "text": CLASSIFICATION_PROMPT,
                        },
                    ],
                },
            ],
            max_tokens=10,
        ),
        caller_label="classify_page_azure",
    )
 
    raw = response.choices[0].message.content.strip().lower()
 
    valid: set[PageRoute] = {"flowchart", "map", "sop", "skip"}
    if raw not in valid:
        logger.warning(
            f"classify_page_azure got unexpected label "
            f"'{raw}' for {page_image_path} — defaulting to 'sop'"
        )
        return "sop"
 
    logger.info(
        f"classify_page_azure | label={raw} | "
        f"image={Path(page_image_path).name}"
    )
    return raw 

# def classify_page_openai(
#     page_image_path: str,
#     client: AzureOpenAI,
# ) -> PageRoute:
#     """
#     Classify a page image into a pipeline route using Azure OpenAI GPT-5.1.

#     Args:
#         page_image_path: Path to the rendered page PNG.
#         client: AzureOpenAI client.

#     Returns:
#         One of: "flowchart", "map", "sop", "skip"
#     """

#     b64, media_type = _encode_image(page_image_path)

#     response = client.chat.completions.create(
#         model=OPENAI_MODEL,
#         messages=[
#             {
#                 "role": "system",
#                 "content": SYSTEM_PROMPT,
#             },
#             {
#                 "role": "user",
#                 "content": [
#                     {
#                         "type": "image_url",
#                         "image_url": {
#                             "url": f"data:{media_type};base64,{b64}",
#                         },
#                     },
#                     {
#                         "type": "text",
#                         "text": CLASSIFICATION_PROMPT,
#                     },
#                 ],
#             },
#         ],
#         max_tokens=10,
#     )

#     raw = response.choices[0].message.content.strip().lower()

#     valid: set[PageRoute] = {
#         "flowchart",
#         "map",
#         "sop",
#         "skip",
#     }

#     if raw not in valid:
#         logger.warning(
#             f"classify_page_azure got unexpected label "
#             f"'{raw}' for {page_image_path} — defaulting to 'sop'"
#         )
#         return "sop"

#     logger.info(
#         f"classify_page_azure | label={raw} | "
#         f"image={Path(page_image_path).name}"
#     )

#     return raw  # type: ignore[return-value]
