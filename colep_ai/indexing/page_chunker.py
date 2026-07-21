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
    legend = data.get("legend", [])

    # flowchart block lives under data["flowchart"], not data directly
    flowchart = data.get("flowchart", {})
    nodes = flowchart.get("nodes", [])

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

        # table row fallback
        if not pt:
            fields = entry.get("fields", {})
            if fields:
                pt = " | ".join(f"{k} {v}" for k, v in fields.items())
                en = pt

        if pt:
            text_pt_parts.append(pt)
        if en:
            text_en_parts.append(en)
        if img:
            image_desc_parts.append(img)

    # flowchart fallback — only when no entry text found
    if nodes and not text_pt_parts and not text_en_parts:
        pt_desc = flowchart.get("flow_chart_description", "").strip()
        en_desc = flowchart.get("flow_chart_description_en", "").strip()
        if pt_desc:
            text_pt_parts.append(pt_desc)
        if en_desc:
            text_en_parts.append(en_desc)
        # node labels as supplementary text
        node_labels = " | ".join(
            n["label"] for n in nodes if isinstance(n, dict) and n.get("label")
        )
        if node_labels:
            text_pt_parts.append(node_labels)
            text_en_parts.append(node_labels)

    if not text_pt_parts and not text_en_parts and not image_desc_parts:
        pt_desc = flowchart.get("flow_chart_description", "").strip()
        en_desc = flowchart.get("flow_chart_description_en", "").strip()
        if pt_desc:
            text_pt_parts.append(pt_desc)
            logger.info(f"Flowchart description (pt) used for {source_file} page {page_number}")
        if en_desc:
            text_en_parts.append(en_desc)
            logger.info(f"Flowchart description (en) used for {source_file} page {page_number}")
        if nodes:
            node_labels = " | ".join(
                n["label"] for n in nodes if isinstance(n, dict) and n.get("label")
            )
            if node_labels:
                text_pt_parts.append(node_labels)
                text_en_parts.append(node_labels)
                logger.info(f"Flowchart node labels appended for {source_file} page {page_number}: {len(nodes)} nodes")

    document_code = data.get("document_code", "")
    document_title = data.get("document_title", "")

    # line_number: always store as JSON string — list[int] or "" both serialise cleanly
    line_number = data.get("line_number") or []

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
            "line_number": line_number,            # list[int] e.g. [28,34,95] or []
            "page_image_ids": data.get("page_image_ids", []),
            "entries": entries,
            "legend": legend,
            "flowchart": flowchart,
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