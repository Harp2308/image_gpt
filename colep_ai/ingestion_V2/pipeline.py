"""
Pipeline: Excel -> PDF -> page PNGs -> OCR -> cropped images -> Claude
association -> per-page JSON.

Excel->PDF conversion (win32com) happens ONCE per document, cached by
checking pdf existence. Same for the full page-PNG render. Calling
run_pipeline() repeatedly for different pages of the same doc will NOT
re-trigger Excel COM or re-render pages already on disk.
"""
import json
import logging
from pathlib import Path

import anthropic
from google.cloud import vision

from colep_ai.core.config import settings
from colep_ai.ingestion_V2.excel_to_image import excel_to_pdf, pdf_to_images
from colep_ai.ingestion_V2.ocr_extractor import image_to_vision_json
from colep_ai.ingestion_V2.pdf_to_images import extract_images_from_page
from colep_ai.ingestion_V2.xlsx_group_resolver import XlsxGroupResolver
from colep_ai.ingestion_V2.image_group_resolver import associate_ocr_with_images
from colep_ai.ingestion_V2.image_combiner import reconstruct_all_steps,group_image_ids_by_step
logger = logging.getLogger(__name__)


def _ensure_pdf(excel_path: Path, source_file: str) -> Path:
    pdf_path = settings.pdf_dir(source_file) / f"{source_file}.pdf"
    if pdf_path.exists():
        logger.info("Stage 1 skipped (cached) | pdf: %s", pdf_path)
        return pdf_path
    result_path = excel_to_pdf(str(excel_path), str(settings.pdf_dir(source_file)))
    logger.info("Stage 1 done | pdf: %s", result_path)
    return Path(result_path)


def _ensure_page_image(pdf_path: Path, source_file: str, page_number: int) -> Path:
    page_img = settings.page_images_dir(source_file) / f"{source_file}_page_{page_number}.png"
    if page_img.exists():
        return page_img
    # renders ALL pages once; cheap relative to Excel export, cached after first call
    pdf_to_images(str(pdf_path), str(settings.page_images_dir(source_file)))
    if not page_img.exists():
        raise RuntimeError(f"Expected page image not found after render: {page_img}")
    return page_img


def run_pipeline(
    excel_path: str,
    page_number: int,  # 1-based, interface contract
    vision_client: vision.ImageAnnotatorClient,
    claude_client: anthropic.Anthropic,
) -> dict:
    excel_path = Path(excel_path)
    source_file = excel_path.stem
    page_idx = page_number - 1  # 0-based, internal only past this point

    settings.ensure_doc_dirs(source_file)

    # Stage 1: Excel -> PDF (cached, once per doc)
    pdf_path = _ensure_pdf(excel_path, source_file)

    # Stage 2: PDF -> page PNG (cached, once per doc, all pages rendered together)
    page_img = _ensure_page_image(pdf_path, source_file, page_number)

    # Stage 3: OCR on target page
    ocr_json_path = image_to_vision_json(str(page_img), str(settings.ocr_dir(source_file)), vision_client)
    with open(ocr_json_path, encoding="utf-8") as f:
        ocr_data = json.load(f)
    logger.info("Stage 3 done | blocks: %d", len(ocr_data["blocks"]))

    # Stage 4: CV crops with xlsx-group-aware union merge
    page_crops_dir = settings.crops_dir(source_file) / f"page_{page_number}"
    group_resolver = XlsxGroupResolver(str(excel_path))
    crops_metadata = extract_images_from_page(
        pdf_path=str(pdf_path),
        page_number=page_idx,
        dpi=200,
        output_dir=str(page_crops_dir),
        group_resolver=group_resolver,
    )
    marked_image_path = page_crops_dir / "marked" / f"page_{page_number}_marked.png"
    logger.info("Stage 4 done | crops: %d, marked: %s", len(crops_metadata), marked_image_path)

    if not crops_metadata:
        logger.warning("No images extracted on page %d — skipping Claude association", page_number)
        result = {"results": [], "warning": "no_images_extracted"}
    else:
        # Stage 5: Claude association -> structured JSON
        result = associate_ocr_with_images(
            marked_image_path=str(marked_image_path),
            ocr_full_text=ocr_data["full_text"],
            crops_metadata=crops_metadata,
            source_file=source_file,
            page_number=page_number,
            client=claude_client,
            model=settings.anthropic_model,
        )
        out_path = settings.results_dir(source_file) / f"{source_file}_page_{page_number}_result.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info("Saved: %s", out_path)

        logger.info("Stage 5 done | results: %d", len(result.get("results", [])))


    # Stage 6: reconstruct grouped step images
    grouped = group_image_ids_by_step(result.get("results", []))
    recon_status = reconstruct_all_steps(
            grouped=grouped,
            page_num=page_number,
            crops_root=settings.crops_dir(source_file),
            output_dir=settings.combined_dir(source_file),
        )
    failed_steps = [k for k, v in recon_status.items() if not v]
    if failed_steps:
        logger.warning(f"Stage 6: reconstruction failed for steps {failed_steps}")
    result["reconstruction_status"] = recon_status
    logger.info("Stage 6 done | steps: %d, failed: %d", len(recon_status), len(failed_steps))

    

    return "result"
