# import anthropic, base64, json

# def associate_ocr_with_images(
#     marked_image_path: str,
#     ocr_full_text: str,
#     crops_metadata: list[dict],
#     source_file: str,
#     page_number: int,
#     client: anthropic.Anthropic,
# ) -> dict:

#     with open(marked_image_path, "rb") as f:
#         img_b64 = base64.standard_b64encode(f.read()).decode()

#     system = """You are a document parser for Portuguese manufacturing SOPs.
# Your job: map each image region to its step number and instruction text.

# Rules:
# - Use ONLY the OCR text provided. Do not invent, translate, or paraphrase Portuguese terms.
# - Each image in the marked image has a unique ID label printed directly on it in blue text — 
#   format is: page_4_XXXX (e.g. page_4_09ea, page_4_2f8a). 
#   Read this label exactly as printed. Do NOT use sequential numbers like 1, 2, 3.
# - Match image_id labels visible in the marked image to nearby step numbers and text.
# - source_text must be copied character-for-character from the OCR — no rewording.
# - If you cannot confidently associate an image, set step_number to null.
# - Return only valid JSON. No explanation, no markdown.


# === IMAGE DESCRIPTION GUIDELINES ===

# Write image_description as a structured paragraph covering ALL of the following:

# 1. MAIN SCENE (1-2 sentences)
#    - What equipment, object, or environment is shown?
#    - What is the overall context (e.g., control panel, workstation, tool in use)?

# 2. RED-MARKED REGION (2-3 sentences)
#    - What specific element is circled, arrowed, or highlighted in red?
#    - Describe its appearance: color, shape, position, label, or text if readable.
#    - What action or state does it represent?
   
# 3. INSET / ZOOM / DETAIL VIEWS (if present)
#    - Is there a magnified close-up, secondary panel, or cutaway view?
#    - What does the inset emphasize that the main image might obscure?

# 4. VISUAL COMMUNICATION /CONNECTION TO STEP:  (1-2 sentences)
#    - How do the marks (circles, arrows, boxes) guide the operator?
#    - What ambiguity do they resolve?
#    -How does what is visually shown relate to the source_text instruction for this step? Be explicit — e.g. "The red circle highlights the STOP button referenced in the step instruction."


# 5. TEXT IN IMAGE (if any text is visible inside the image itself, not OCR)
#    - Any labels, signs, screen readouts, or button text visible in the photo.

# Tone: Technical, precise, objective. Do not infer beyond what is visually evident.
# Length: 60-100 words. Be thorough but concise.

# """
#     image_ids = [c["image_id"] for c in crops_metadata]
    
#     user = f"""OCR full text (ground truth, do not deviate):
# {ocr_full_text}

# These are the ONLY valid image_ids on this page — use exactly these, no others:
# {json.dumps(image_ids, indent=2)}

# source_file: {source_file}
# page_number: {page_number}

# For each image_id visible in the marked image, return:
# {{
#   "results": [
#     {{
#       "image_id": "<label from marked image>",
#       "step_number": <int or null>,
#       "source_text": "<exact Portuguese text from OCR>",
#       "translated_text": "<English translation>",
#       "image_description": "<see detailed guidelines >",
#       "metadata": {{
#         "page": {page_number},
#         "source_file": "{source_file}",
#         "line": "<from OCR footer>",
#         "doc_type": "<from OCR footer>"
#       }}
#     }}
#   ]
# }}"""

#     response = client.messages.create(
#         model="claude-sonnet-4-6",
#         max_tokens=10000,
#         system=system,
#         messages=[{
#             "role": "user",
#             "content": [
#                 {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
#                 {"type": "text", "text": user},
#             ],
#         }],
#     )

#     raw = response.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
#     return json.loads(raw)


import anthropic, base64, json

def associate_ocr_with_images(
    marked_image_path: str,
    ocr_full_text: str,
    crops_metadata: list[dict],
    source_file: str,
    page_number: int,
    client: anthropic.Anthropic,
) -> dict:

    with open(marked_image_path, "rb") as f:
        img_b64 = base64.standard_b64encode(f.read()).decode()

    system = """You are a document parser for Portuguese manufacturing SOPs.
Your job: map each image region to its step number and instruction text.

Rules:
- Use ONLY the OCR text provided. Do not invent, translate, or paraphrase Portuguese terms.
- Each image in the marked image has a unique ID label printed directly on it in blue text — 
  format is: page_4_XXXX (e.g. page_4_09ea, page_4_2f8a). 
  Read this label exactly as printed. Do NOT use sequential numbers like 1, 2, 3.
- Match image_id labels visible in the marked image to nearby step numbers and text.
- source_text must be copied character-for-character from the OCR — no rewording.
- If you cannot confidently associate an image, set step_number to null.
- Return only valid JSON. No explanation, no markdown.

Your PRIMARY task is spatial association, not semantic matching.

Determine which step each image belongs to using the document layout.

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

Never assign an image to a step based only on what the image depicts.

COMMON ERROR TO AVOID: An image positioned near or touching another step's
image cluster is NOT automatically part of that cluster. Check whether a
step-number circle lies between this image and the step it is being
tentatively grouped with. If yes, it belongs to the step whose circle is
closest without another circle in between — not the visually nearest cluster.

=== PAGE LAYOUT RULES ===

The page is a printed SOP, and the layout is NOT fixed (columns/rows vary by page).

Each numbered step consists of:

- a green circled step number
- one Portuguese instruction
- one or more than one photographs

A photograph belongs to the closest numbered instruction that visually owns it,
verified strictly through the step-number boundary check in rule 1 above.

Ownership is determined by page layout, NOT by image contents.

Important:

- Multiple photos may belong to one step.
- Photos for the same step may be stacked vertically.
- Photos may extend BELOW the instruction text.
- A photo directly below another photo may still belong to the same step,
  but ONLY if no step-number circle for a different step lies between them.
- Never assume a new nearby step "captures" a photo unless it is clearly
  inside that step's visual region, per the boundary check.

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

Tone: Technical, precise, objective. Do not infer beyond what is visually evident.
Length: 60-100 words. Be thorough but concise.

=== MANDATORY SELF-CHECK BEFORE OUTPUT ===

Before finalizing output, re-verify every image_id assignment:
For each image, confirm no other step-number circle sits between it and
its assigned step's circle. If one does, reassign to the correct step.
Do this check for every image_id independently — do not assume correctness
from clustering.
"""

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
      "image_description": "<see detailed guidelines >",
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
        model="claude-sonnet-4-6",
        max_tokens=10000,
        system=system,
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
    


