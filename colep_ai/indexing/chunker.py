import json
import re
import uuid
from pathlib import Path
from typing import Optional


from colep_ai.core.logger import get_logger

logger = get_logger(__name__)




def _point_id(source_file: str, page_number: int, entry_id: str) -> str:
    raw = f"{source_file}:{page_number}:{entry_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, raw))

def _embedding_text(entry: dict) -> str:
    parts = [
        entry.get("entry_text", ""),
        entry.get("entry_text_en", ""),
        entry.get("image_description", ""),
    ]
    return " ".join(p.strip() for p in parts if p.strip())

# def chunk_page_json(data: dict, source_file: str, page_number: int) -> list[dict]:
#     chunks = []
#     for entry in data.get("entries", []):
#         text = _embedding_text(entry)
#         if not text:
#             continue
#         chunks.append({
#             "id": _point_id(source_file, page_number, entry["entry_id"]),
#             "text": text,
#             "payload": {
#                 "source_file": source_file,
#                 "page_number": page_number,
#                 "document_title": data.get("document_title", ""),
#                 "document_code": data.get("document_code", ""),
#                 "entry_id": entry.get("entry_id", ""),
#                 "parent_id": entry.get("parent_id", ""),
#                 "entry_text": entry.get("entry_text", ""),
#                 "entry_text_en": entry.get("entry_text_en", ""),
#                 "image_ids": entry.get("image_ids", []),
#                 "image_description": entry.get("image_description", ""),
#                 "fields": entry.get("fields", {}),
#             },
#         })
#     return chunks

def chunk_page_json(data: dict, source_file: str, page_number: int) -> list[dict]:
    chunks = []
    for entry in data.get("entries", []):
        if not isinstance(entry, dict):
            logger.warning(
                f"Skipping malformed entry (not dict) in {source_file} page {page_number}:{entry}",
                
            )
            continue
        text = _embedding_text(entry)
        if not text:
            continue
        chunks.append({
            "id": _point_id(source_file, page_number, entry["entry_id"]),
            "text": text,
            "payload": {
                "source_file": source_file,
                "page_number": page_number,
                "document_title": data.get("document_title", ""),
                "document_code": data.get("document_code", ""),
                "entry_id": entry.get("entry_id", ""),
                "parent_id": entry.get("parent_id", ""),
                "entry_text": entry.get("entry_text", ""),
                "entry_text_en": entry.get("entry_text_en", ""),
                "image_ids": entry.get("image_ids", []),
                "image_description": entry.get("image_description", ""),
                "fields": entry.get("fields", {}),
            },
        })
    return chunks

def chunk_all_pages(results_dir: Path, source_file: str | None = None) -> list[dict]:
    FILENAME_RE = re.compile(r"^(.*)_page_(\d+)_result\.json$")
    results_dir = Path(results_dir)
    if source_file is None:
        source_file = results_dir.parent.name

    logger.info(f"Chunking pages for source_file={source_file} in {results_dir}")

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
        page_chunks = chunk_page_json(data, source_file, page_number)
        logger.info(f"{f.name}: {len(page_chunks)} chunks")
        all_chunks.extend(page_chunks)

    logger.info(f"Total chunks: {len(all_chunks)} for source_file={source_file}")
    return all_chunks



# def chunk_all_pages(results_dir: Path, source_file: Optional[str]) -> list[dict]:
#     all_chunks = []
#     for f in sorted(results_dir.glob(f"{source_file}_page_*_result.json")):
#         m = FILENAME_RE.match(f.name)
#         if not m:
#             continue
#         page_number = int(m.group(2))
#         data = json.loads(f.read_text(encoding="utf-8"))
#         all_chunks.extend(chunk_page_json(data, source_file, page_number))
#     return all_chunks

# def chunk_all_pages(results_dir: Path, source_file: str | None = None) -> list[dict]:
#     FILENAME_RE = re.compile(r"^(.*)_page_(\d+)_result\.json$")
#     results_dir = Path(results_dir)
#     if source_file is None:
#         # outputs/{source_file}/results -> parent of results_dir is source_file
#         source_file = results_dir.parent.name

#     all_chunks = []
#     for f in sorted(results_dir.glob(f"{source_file}_page_*_result.json")):
#         m = FILENAME_RE.match(f.name)
#         if not m:
#             continue
#         page_number = int(m.group(2))
#         data = json.loads(f.read_text(encoding="utf-8"))
#         all_chunks.extend(chunk_page_json(data, source_file, page_number))
#     return all_chunks