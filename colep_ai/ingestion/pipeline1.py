"""
Pipeline: Excel -> PDF -> page PNGs -> OCR -> cropped images -> Claude
association -> per-page JSON.

Excel->PDF conversion (win32com) happens ONCE per document, cached by
checking pdf existence. Same for the full page-PNG render. Calling
run_pipeline() repeatedly for different pages of the same doc will NOT
re-trigger Excel COM or re-render pages already on disk.
"""
import json
from pathlib import Path
import pymupdf as fitz
import anthropic
from google.cloud import vision
import re
import time

from colep_ai.core.config import settings
from colep_ai.ingestion.excel_to_image2 import excel_to_pdf, pdf_to_images, word_to_pdf
from colep_ai.ingestion.ocr_extractor import image_to_vision_json
from colep_ai.ingestion.pdf_to_images import extract_images_from_page
from colep_ai.ingestion.xlsx_group_resolver import XlsxGroupResolver
from colep_ai.ingestion.image_group_resolver import extract_steps
from colep_ai.ingestion.image_combiner import reconstruct_all_steps,group_image_ids_by_entry
from colep_ai.ingestion.utils import normalize_filename,_log_stage,normalize_result_schema
from colep_ai.core.logger import get_logger
from colep_ai.ingestion.flowchart_extractor import extract_flowchart
from colep_ai.ingestion.map_extractor import extract_map_zones
from colep_ai.ingestion.page_classifier import  classify_page_azure

logger = get_logger("Ingestion_pipeline")


def _load_sheet_name(source_file: str, page_number: int) -> str:
    """
    Read the sheet_page_map sidecar JSON built by excel_to_pdf and return
    the sheet name for the given PDF page number.

    Returns empty string if the map doesn't exist (e.g. Word documents,
    cached PDFs from before this feature, or any non-Excel source).
    The empty string is safe — it means 'sheet_name' in result.json will
    be "" rather than missing, which indexers handle without change.
    """
    sheet_map_path = settings.pdf_dir(source_file) / f"{source_file}.sheet_map.json"
    if not sheet_map_path.exists():
        return ""
    try:
        sheet_map = json.loads(sheet_map_path.read_text(encoding="utf-8"))
        return sheet_map.get(str(page_number), "")
    except Exception:
        return ""






def _ensure_pdf(excel_path: Path, source_file: str, folder_name: str) -> Path:
    pdf_path = settings.pdf_dir(source_file) / f"{source_file}.pdf"
    if pdf_path.exists():
        logger.info(f"Stage 1 skipped (cached) | pdf: {pdf_path}")
        return pdf_path

    ext = excel_path.suffix.lower()
    if ext in (".docx", ".doc"):
        result_path = word_to_pdf(str(excel_path), str(settings.pdf_dir(source_file)), source_file, folder_name)
    else:
        result_path = excel_to_pdf(str(excel_path), str(settings.pdf_dir(source_file)), source_file, folder_name)

    logger.info(f"Stage 1 done | pdf: {result_path}")
    return Path(result_path)


def _ensure_page_image(pdf_path: Path, source_file: str, page_number: int, folder_name: str) -> Path:
    page_img = settings.page_images_dir(source_file) / f"{source_file}_page_{page_number}.png"
    if page_img.exists():
        return page_img
    # renders ALL pages once; cheap relative to Excel export, cached after first call
    pdf_to_images(str(pdf_path), str(settings.page_images_dir(source_file)), source_file, folder_name)
    if not page_img.exists():
        raise RuntimeError(f"Expected page image not found after render: {page_img}")
    return page_img

def get_total_pages(excel_path: str) -> int:
    excel_path = Path(excel_path)
    source_file = normalize_filename(excel_path.stem)   # <-- fix: was raw .stem, now matches run_pipeline
    settings.ensure_doc_dirs(source_file)
    pdf_path = _ensure_pdf(excel_path, source_file, folder_name="")
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def extract_line_number(stem: str) -> list[int] | None:
    # Strip doc code prefix: Q01_L132_3_ or O01_O119_1_
    stripped = re.sub(r'^[A-Z]\d+_[A-Z]\d+_\d+_', '', stem, flags=re.IGNORECASE)

    # Match "Linhas_28_34_e_95" OR "L19_e_83" OR "Linha_19" OR "L13"
    match = re.search(r'(?:Linhas?|L)[_ ]*((?:\d+[_ e,]*)+)', stripped, re.IGNORECASE)
    if not match:
        return None

    return [int(n) for n in re.findall(r'\d+', match.group(1))]


def run_pipeline(
    excel_path: str,
    page_number: int,  # 1-based, interface contract,
    claude_client: anthropic.AsyncAnthropic,
    openai_client=None,
    folder_name: str = "",

) -> dict:
    pipeline_start = time.monotonic()
    t = time.monotonic()
    excel_path = Path(excel_path)
    source_file = normalize_filename(excel_path.stem)
    page_idx = page_number - 1  # 0-based, internal only past this point

    settings.ensure_doc_dirs(source_file)

    # Stage 1: Excel -> PDF (cached, once per doc)
    pdf_path = _ensure_pdf(excel_path, source_file, folder_name)
    _log_stage("1 excel_to_pdf", t); t = time.monotonic()

    # Stage 2: PDF -> page PNG (cached, once per doc, all pages rendered together)
    page_img = _ensure_page_image(pdf_path, source_file, page_number, folder_name)
    _log_stage("2 page_image", t); t = time.monotonic()

    # Stage 3: Classify page type via LLM vision classifier
    out_path = settings.results_dir(source_file) / f"{source_file}_page_{page_number}_result.json"
    page_route = classify_page_azure(str(page_img),  openai_client)
    _log_stage("3 page_classification", t); t = time.monotonic()

    if page_route == "skip":
        logger.info(f"Stage 3 | skip — empty form/checklist, no extraction | page={page_number}")
        return {"page_number": page_number, "source_file": source_file, "route": "skip"}

    if page_route == "flowchart":
        logger.info("Stage 3 | flowchart detected — routing to flowchart extractor")

        # Stage 4 (flowchart): page image + filename -> LLM -> JSON
        result = extract_flowchart(str(page_img), claude_client)
        result["page_number"] = page_number
        result["source_file"] = source_file
        result["folder_name"] = folder_name
        result["line_number"] = extract_line_number(source_file)
        result["page_image_id"] = [page_img.name]
        
        _log_stage("4 flowchart_extraction", t); t = time.monotonic()
        normalize_result_schema(result)
        result["sheet_name"] = _load_sheet_name(source_file, page_number)
        logger.info(f"line_number={result['line_number']} | source_file={source_file}")

        logger.info(f"Saved: {out_path}")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    elif page_route == "map":
        logger.info("Stage 3 | map detected — routing to map extractor")

        # Stage 4: CV crops (same as SOP — map pages have cropped icons)
        page_crops_dir = settings.crops_dir(source_file) / f"page_{page_number}"
        group_resolver = XlsxGroupResolver(str(excel_path))
        crops_metadata = extract_images_from_page(
            pdf_path=str(pdf_path),
            page_number=page_idx,
            dpi=200,
            output_dir=str(page_crops_dir),
            group_resolver=group_resolver,
            source_file=source_file,
            folder_name=folder_name,
        )
        marked_image_path = page_crops_dir / "marked" / f"page_{page_number}_marked.png"
        _log_stage("4 cv_crops", t); t = time.monotonic()

        # Stage 5: map zone extraction
        map_data = extract_map_zones(
            full_page_image_path=str(marked_image_path) if crops_metadata else str(page_img),
            image_meta=crops_metadata,
            client=claude_client,
        )
        _log_stage("5 map_extraction", t); t = time.monotonic()

        # Stage 6: reconstruct all map image_ids into one combined image
        map_image_ids = map_data.get("map_image_ids") or []
        bbox_index = {m["image_id"]: m["bbox"] for m in crops_metadata if "bbox" in m}
        recon_status = reconstruct_all_steps(
            grouped={"map": map_image_ids} if len(map_image_ids) >= 2 else {},
            page_num=page_number,
            crops_root=settings.crops_dir(source_file),
            output_dir=settings.combined_dir(source_file),
            source_file=source_file,
            folder_name=folder_name,
            bbox_index=bbox_index,
        )
        info = recon_status.get("map", {})
        map_data["is_combined"] = info.get("is_combined", False)
        map_data["combined_image"] = info.get("combined_image", None)
        _log_stage("6 map_image_reconstruction", t); t = time.monotonic()

        result = {}
        result["document_title"] = map_data.pop("document_title", "")
        result["document_code"] = map_data.pop("document_code", "")
        result["map"] = map_data
        result["page_number"] = page_number
        result["source_file"] = source_file
        result["folder_name"] = folder_name
        result["line_number"] = extract_line_number(source_file)
        result["page_image_id"] = [page_img.name]
        normalize_result_schema(result)
        result["sheet_name"] = _load_sheet_name(source_file, page_number)
        logger.info(f"line_number={result['line_number']} | source_file={source_file}")

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved: {out_path}")

    else:  # page_route == "sop"
        logger.info("Stage 3 | sop — routing to standard pipeline")

        # Stage 4: CV crops with xlsx-group-aware union merge
        page_crops_dir = settings.crops_dir(source_file) / f"page_{page_number}"
        group_resolver = XlsxGroupResolver(str(excel_path))
        crops_metadata = extract_images_from_page(
            pdf_path=str(pdf_path),
            page_number=page_idx,
            dpi=200,
            output_dir=str(page_crops_dir),
            group_resolver=group_resolver,
            source_file=source_file, 
            folder_name=folder_name,
        )
        marked_image_path = page_crops_dir / "marked" / f"page_{page_number}_marked.png"
        logger.info(f"Stage 4 done | crops: {len(crops_metadata)}, marked: {marked_image_path}")
        _log_stage("4 cv_crops", t); t = time.monotonic()

        # Stage 5: Claude association -> structured JSON
        if not crops_metadata:
            logger.warning(f"No images extracted on page {page_number} — using raw page image association")
            result = extract_steps(
                full_page_image_path=str(page_img),
                image_meta=[],
                client=claude_client,
            )
            result["page_image_id"] = [page_img.name]
            
        else:
            result = extract_steps(full_page_image_path=str(marked_image_path), image_meta=crops_metadata, client=claude_client)
           
            logger.info(f"Stage 5 done | entries: {len(result.get('entries', []))}")
            _log_stage("5 claude_extraction", t); t = time.monotonic()

        # Stage 6: reconstruct grouped step images
        grouped = group_image_ids_by_entry(result.get("entries", []))
        bbox_index = {m["image_id"]: m["bbox"] for m in crops_metadata if "bbox" in m}
        recon_status = reconstruct_all_steps(
            grouped=grouped,
            page_num=page_number,
            crops_root=settings.crops_dir(source_file),
            output_dir=settings.combined_dir(source_file),
            source_file=source_file,
             folder_name=folder_name,
            bbox_index=bbox_index,
        )

        for entry in result.get("entries", []):
            entry_id = entry["entry_id"]
            if not entry.get("image_ids"):
                entry["is_combined"] = False
                entry["combined_image"] = None
                continue
            info = recon_status.get(entry_id, {})
            entry["is_combined"] = info.get("is_combined", False)
            entry["combined_image"] = info.get("combined_image", None)

        failed_steps = [k for k, v in recon_status.items() if v.get("success") is False]
        if failed_steps:
            logger.warning(f"Stage 6: reconstruction failed for steps {failed_steps}")
            
        normalize_result_schema(result)
        result["page_number"] = page_number
        result["source_file"] = source_file
        result["folder_name"] = folder_name
        result["line_number"] = extract_line_number(source_file)
        result["sheet_name"] = _load_sheet_name(source_file, page_number)
        logger.info(f"line_number={result['line_number']} | source_file={source_file}")

        logger.info(f"Saved: {out_path}")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info(f"Stage 6 done | steps: {len(recon_status)}, failed: {len(failed_steps)}")
        _log_stage("6 image_reconstruction", t); t = time.monotonic()

    logger.info(f"Pipeline complete | total={time.monotonic() - pipeline_start:.2f}s | source_file={source_file} | page={page_number}")
    return result


# if __name__=="__main__":
#     from colep_ai.generation.claude_client import get_claude_client
#     excel_path=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV2\documents\O01.O022.1 - OPL - Parametrizar LD188 sem poliuretano (PU) Linha 35.xlsx"
#     page_number=1
#     client=get_claude_client()

#     run_pipeline(excel_path,page_number,client)