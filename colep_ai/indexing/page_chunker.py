import json
import re
import uuid
from pathlib import Path

from colep_ai.core.logger import get_logger

logger = get_logger(__name__)


def _page_point_id(document_code: str, document_title: str, source_file: str, page_number: int) -> str:
    raw = f"{document_code}:{document_title}:{source_file}:{page_number}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, raw))


def chunk_page_json_full(data: dict, source_file: str, page_number: int) -> dict | None:
    entries = data.get("entries", [])

    text_pt_parts = []
    text_en_parts = []
    image_desc_parts = []

    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning(f"Skipping malformed entry (not dict) in {source_file} page {page_number}: {entry!r}")
            continue

        pt = entry.get("entry_text", "").strip()
        en = entry.get("entry_text_en", "").strip()
        img = entry.get("image_description", "").strip()

        if pt:
            text_pt_parts.append(pt)
        if en:
            text_en_parts.append(en)
        if img:
            image_desc_parts.append(img)

    if not text_pt_parts and not text_en_parts and not image_desc_parts:
        logger.warning(f"No embeddable text found in {source_file} page {page_number} — skipping")
        return None

    document_code = data.get("document_code", "")
    document_title = data.get("document_title", "")

    return {
        "id": _page_point_id(document_code, document_title, source_file, page_number),
        "text_pt": " ".join(text_pt_parts),
        "text_en": " ".join(text_en_parts),
        "image_desc": " ".join(image_desc_parts),
        "payload": {
            "source_file": source_file,
            "page_number": page_number,
            "document_title": document_title,
            "document_code": document_code,
            "entries": entries,
        },
    }


def chunk_all_pages_full(results_dir: Path, source_file: str | None = None) -> list[dict]:
    FILENAME_RE = re.compile(r"^(.*)_page_(\d+)_result\.json$")
    results_dir = Path(results_dir)

    if source_file is None:
        source_file = results_dir.parent.name

    logger.info(f"Page-based chunking: source_file={source_file} dir={results_dir}")

    matched_files = sorted(results_dir.glob(f"{source_file}_page_*_result.json"))
    if not matched_files:
        logger.warning(f"No result files matched pattern '{source_file}_page_*_result.json' in {results_dir}")
        return []

    all_chunks = []
    for f in matched_files:
        m = FILENAME_RE.match(f.name)
        if not m:
            logger.warning(f"Skipping file with unexpected name format: {f.name}")
            continue

        page_number = int(m.group(2))

        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in {f.name}: {e}")
            continue

        chunk = chunk_page_json_full(data, source_file, page_number)
        if chunk is None:
            continue

        logger.info(f"{f.name} -> page chunk id={chunk['id']}")
        all_chunks.append(chunk)

    logger.info(f"Total page chunks: {len(all_chunks)} for source_file={source_file}")
    return all_chunks