import json
import logging
from pathlib import Path
import anthropic
from dotenv import load_dotenv
import os


from colep_ai.ingestion.excel_to_image import excel_to_images
from colep_ai.ingestion.ocr_extractor import get_vision_client,image_to_vision_json
from colep_ai.ingestion.pdf_to_images import extract_images_from_page
from colep_ai.ingestion.image_group_resolver import associate_ocr_with_images
from colep_ai.ingestion.image_combiner import group_image_ids_by_step, reconstruct_all_steps
from colep_ai.core.config import settings
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

settings.ensure_dirs()
 
def run_pipeline(excel_path: str, page_number: int, vision_client, claude_client) -> dict:
    """
    excel_path  : path to .xlsx
    page_number : 1-based
    """
    excel_path  = Path(excel_path)
    source_file = excel_path.stem
    page_idx    = page_number - 1  # 0-based for internal use
 
    # Stage 1: Excel -> PDF -> page images
    pdf_path = settings.pdfs_dir / f"{source_file}.pdf"
    page_img = settings.page_images_dir / f"{source_file}_page_{page_number}.png"
 
    if not pdf_path.exists():
        excel_to_images(str(excel_path), str(settings.pdfs_dir), str(settings.page_images_dir))
    logger.info("Stage 1 done | pdf: %s", pdf_path)
 
    # Stage 2: OCR on target page only
    ocr_json_path = image_to_vision_json(str(page_img), str(settings.output_dir / "ocr"), vision_client)
    with open(ocr_json_path, encoding="utf-8") as f:
        ocr_data = json.load(f)
    logger.info("Stage 2 done | blocks: %d", len(ocr_data["blocks"]))
 
    # Stage 3: CV crops on target page only — isolated subfolder per page
    page_crops_dir = settings.crops_dir / f"page_{page_number}"
    crops_metadata = extract_images_from_page(
        pdf_path=str(pdf_path),
        page_number=page_idx,
        dpi=200,
        output_dir=str(page_crops_dir),
    )
    marked_image_path = page_crops_dir / "marked" / f"page_{page_number}_marked.png"
    logger.info("Stage 3 done | marked: %s", marked_image_path)
 
    return
    # Stage 4: Claude association
    # result = associate_ocr_with_images(
    #     marked_image_path=str(marked_image_path),
    #     ocr_full_text=ocr_data["full_text"],
    #     crops_metadata=crops_metadata,
    #     source_file=source_file,
    #     page_number=page_number,
    #     client=claude_client,
    # )
    # logger.info("Stage 4 done | results: %d", len(result.get("results", [])))
 
    # # Stage 5: reconstruct grouped step images
    # grouped = group_image_ids_by_step(result.get("results", []))
    # recon_status = reconstruct_all_steps(
    #     grouped=grouped,
    #     page_num=page_number,
    #     crops_root=settings.crops_dir,
    #     output_dir=settings.reconstructed_dir,
    # )
    # failed_steps = [k for k, v in recon_status.items() if not v]
    # if failed_steps:
    #     logger.warning(f"Stage 5: reconstruction failed for steps {failed_steps}")
    # result["reconstruction_status"] = recon_status
    # logger.info("Stage 5 done | steps: %d, failed: %d", len(recon_status), len(failed_steps))

    # out_path = settings.results_dir / f"{source_file}_page_{page_number}_result.json"
    # with open(out_path, "w", encoding="utf-8") as f:
    #     json.dump(result, f, ensure_ascii=False, indent=2)

    # return result
 
 
if __name__ == "__main__":
    vision_client = get_vision_client(str(settings.gcp_key_path))
    claude_client = anthropic.Anthropic()
 
    result = run_pipeline(
        excel_path=r"docs\e2.xlsx",
        page_number=6,
        vision_client=vision_client,
        claude_client=claude_client,
    )
    # print(json.dumps(result, ensure_ascii=False, indent=2))


