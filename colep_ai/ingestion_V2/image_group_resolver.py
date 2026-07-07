import base64
import json

import anthropic

SYSTEM_PROMPT1 = """You are a document parser for Portuguese manufacturing SOPs.
Your job: map each image region to its step number and instruction text.

Rules:
- Use ONLY the OCR text provided. Do not invent, translate, or paraphrase Portuguese terms.
- Each image in the marked image has a unique ID label printed directly on it in blue text.
- Match image_id labels visible in the marked image to nearby step numbers and text.
- source_text must be copied character-for-character from the OCR — no rewording.
- If you cannot confidently associate an image, set step_number to null.
- Return only valid JSON. No explanation, no markdown.

Your PRIMARY task is spatial association, not semantic matching.

Association priority (highest to lowest):
1. Step-number boundary check (MANDATORY, overrides all other signals):
   Locate the green circled step number that owns this image by scanning
   upward/backward from the image until you hit the FIRST step-number circle.
   If ANY other step-number circle appears between the image and a nearby
   image cluster, the image does NOT belong to that cluster's step,
   regardless of visual proximity.
2. Red arrows or connector lines explicitly linking an image to a step's text.
3. Image grouping — ONLY apply this when no step-number circle intervenes
   between grouped images (verified via rule 1).
4. The instruction text nearest to the image.
5. Visual similarity to the instruction (lowest priority, use only as tiebreaker).

=== IMAGE DESCRIPTION GUIDELINES ===
Write image_description as a structured paragraph covering:
1. MAIN SCENE (1-2 sentences)
2. RED-MARKED REGION (2-3 sentences)
3. INSET / ZOOM / DETAIL VIEWS (if present)
4. VISUAL COMMUNICATION / CONNECTION TO STEP (1-2 sentences)
5. TEXT IN IMAGE (if any text is visible inside the image itself, not OCR)

Tone: Technical, precise, objective. Length: 60-100 words.

=== MANDATORY SELF-CHECK BEFORE OUTPUT ===
Before finalizing output, re-verify every image_id assignment: for each image,
confirm no other step-number circle sits between it and its assigned step's
circle. If one does, reassign to the correct step.
"""

SYSTEM_PROMPT = """You are a document parser for Portuguese manufacturing SOPs.
Your job: map each image region to its step number and instruction text, and produce a structured English-annotated record for each image.

=== SOURCE RULES ===
- Use ONLY the OCR text provided. Do not invent, reword, or paraphrase Portuguese terms in source_text.
- source_text must be copied character-for-character from the OCR — no rewording, no cleanup.
- translated_text must be an accurate English translation of source_text — do not alter source_text itself.
- Each image_id is printed in a blue-outlined label box directly on or immediately adjacent to its green-boxed image region in the marked image. Copy image_id exactly as printed — never invent, guess, reformat, or auto-generate an id not visibly present.
- If you cannot confidently associate an image with a step, set step_number to null.
- Return only valid JSON matching the schema below. No explanation, no markdown fences.

=== OVERLAY SEMANTICS ===
- GREEN BOX = a cropped image region. Its blue-outlined label = its image_id.
- RED ARROW between two green-boxed images = the second image is a zoomed-in / cropped detail of the first. Both image_ids belong to the SAME step; include both in that step's image_ids list.
- RED CIRCLE/OVAL drawn on a photo highlights a specific button/valve/switch the instruction refers to. It does NOT create a new image_id — it is the same cropped image, just annotated.
- MULTIPLE PHOTOS grouped under a single blue label box = ONE image_id. Do not split into separate ids unless each photo has its own distinct blue label.
- LETTERS ON BLACK BACKGROUND inside a photo (e.g. boxed "E", "K L M N") are physical control labels visible on the machine — describe these inside image_description, do not create a separate field for them.

=== ASSOCIATION PRIORITY (highest to lowest) ===
Your PRIMARY task is spatial association, not semantic matching.
1. Step-number boundary check (MANDATORY, overrides all other signals): locate the circled step number that owns an image by scanning upward/backward from the image until the FIRST step-number circle is reached. If ANY other step-number circle appears between the image and a nearby image cluster, that image does NOT belong to that cluster's step, regardless of visual proximity.
2. Red-arrow relation: if a red arrow connects image A to image B, both image_ids belong to the same step (B is a zoomed/cropped detail of A) — apply only within the step already determined by rule 1.
3. Image grouping: Multi-image step grouping: if multiple distinct image_ids (each with its own individual blue label) belong to the same step, list all of them together in that step's image_ids array. Apply this only within the step already determined by the step-number boundary check (rule 1) — do not pull an image_id across a step-number circle boundary into this grouping.
Single-label photo grouping: if multiple photos are stacked/grouped under ONE single blue label box, they share ONE image_id. Do not split them into separate ids unless each individual photo has its own distinct blue label.
4. The instruction text nearest to the image.
5. Visual similarity to the instruction (lowest priority, tiebreaker only).

=== IMAGE_DESCRIPTION GUIDELINES ===
Write image_description as a structured paragraph covering:
1. MAIN SCENE (1-2 sentences)
2. RED-MARKED REGION (2-3 sentences) — the specific control/valve/switch circled
3. CROP/DETAIL RELATIONSHIP — if this image_id is linked by a red arrow to another image_id (as parent or as crop), state the relationship and name the other image_id
4. MULTI-PHOTO GROUPING — if this image_id represents multiple photos grouped under one blue label, state that
5. PHYSICAL LABELS — any letter/number control labels on black background visible in the image (e.g. "E", "K L M N")
6. VISUAL COMMUNICATION / CONNECTION TO STEP (1-2 sentences)
7. TEXT IN IMAGE — any text visible inside the image itself, not OCR

Tone: Technical, precise, objective. Length: 60-100 words.

=== MANDATORY SELF-CHECK BEFORE OUTPUT ===
Before finalizing, re-verify every image_id assignment:
- Confirm no other step-number circle sits between an image and its assigned step's circle; reassign if one does.
- Confirm red-arrow pairs are both listed under the same step.
- Confirm grouped multi-photo blocks are collapsed to their single correct image_id, not split.
- Confirm translated_text is a faithful translation of source_text, not a reworded source_text.
"""

def associate_ocr_with_images(
    marked_image_path: str,
    ocr_full_text: str,
    crops_metadata: list[dict],
    source_file: str,
    page_number: int,
    client: anthropic.Anthropic,
    model: str = "claude-sonnet-4-6",
) -> dict:
    with open(marked_image_path, "rb") as f:
        img_b64 = base64.standard_b64encode(f.read()).decode()

    image_ids = [c["image_id"] for c in crops_metadata]

    user = f"""OCR full text (ground truth, do not deviate):
{ocr_full_text}

These are the ONLY valid image_ids on this page — use exactly these, no others:
{json.dumps(image_ids, indent=2)}

source_file: {source_file}
page_number: {page_number}

For each image_id visible in the marked image, return:
{{
  "results": [
    {{
      "image_id": "<label from marked image>",
      "step_number": <int or null>,
      "source_text": "<exact Portuguese text from OCR>",
      "translated_text": "<English translation>",
      "image_description": "<see detailed guidelines>",
      "metadata": {{
        "page": {page_number},
        "source_file": "{source_file}",
        "line": "<from OCR footer>",
        "doc_type": "<from OCR footer>"
      }}
    }}
  ]
}}"""

    response = client.messages.create(
        model=model,
        max_tokens=10000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": user},
            ],
        }],
    )

    raw = response.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(raw)
