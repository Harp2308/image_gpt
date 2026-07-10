import re
from pathlib import Path
from colep_ai.core.logger import get_logger

logger=get_logger(__name__)

def resolve_images(llm_text: str, entries_lookup: dict[tuple[int, str], dict]) -> dict:
    """
    entries_lookup: {(page_number, entry_id): entry_dict}
    entry_dict must have: image_ids, is_combined, combined_image
    """
    pattern = re.compile(r"🖼️\[page (\d+) \| entry (\w+)\]")
    images = []

    for match in pattern.finditer(llm_text):
        page_num = int(match.group(1))
        entry_id = match.group(2)
        key = (page_num, entry_id)

        entry = entries_lookup.get(key)
        if entry is None:
            logger.warning(f"No entry found for [page {page_num} | entry {entry_id}] — skipping image")
            continue

        if entry.get("is_combined"):
            image_ref = entry["combined_image"]
        elif entry.get("image_ids"):
            image_ref = entry["image_ids"][0]
        else:
            image_ref = None

        images.append({
            "marker": f"[page {page_num} | entry {entry_id}]",
            "image_ref": image_ref,
        })

    return {
        "text": llm_text,
        "images": images,
    }