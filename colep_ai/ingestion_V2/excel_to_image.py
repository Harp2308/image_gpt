"""
Excel -> PDF via win32com (requires a live Excel install, Windows only).
Call excel_to_pdf ONCE per document — spinning up Excel.Application per
page is not viable (COM startup cost, single-threaded, file locking).
"""

from pathlib import Path
import fitz
import win32com.client

from colep_ai.core.logger import get_logger

logger = get_logger(__name__)



def excel_to_pdf(excel_path: str, pdf_dir: str) -> str:
    excel_path = str(Path(excel_path).resolve())
    pdf_dir_p = Path(pdf_dir).resolve()
    pdf_dir_p.mkdir(parents=True, exist_ok=True)
    pdf_path = str(pdf_dir_p / f"{Path(excel_path).stem}.pdf")

    excel = win32com.client.Dispatch("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    try:
        wb = excel.Workbooks.Open(excel_path)
        normalize_sheet_pagination(wb,logger)
        try:
            wb.ExportAsFixedFormat(0, pdf_path)  # 0 = xlTypePDF
            
        finally:
            wb.Close(False)
    finally:
        excel.Quit()

    if not Path(pdf_path).exists():
        raise RuntimeError(f"Excel export failed, no PDF at {pdf_path}")

    logger.info(f"excel_to_pdf:{excel_path} ->{pdf_path}")
    return pdf_path


def pdf_to_images(pdf_path: str, output_dir: str, dpi: int = 200) -> list[str]:
    output_dir_p = Path(output_dir)
    output_dir_p.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    image_paths = []
    try:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix)
            img_path = output_dir_p / f"{Path(pdf_path).stem}_page_{i}.png"
            pix.save(str(img_path))
            image_paths.append(str(img_path))
    finally:
        doc.close()

    logger.info("pdf_to_images: %s -> %d pages", pdf_path, len(image_paths))
    return image_paths


def normalize_sheet_pagination(wb, logger) -> dict:
    """
    Force fit-to-page ONLY on sheets that actually overflow one page.
    Leaves correctly-configured sheets (any zoom) untouched.
    Must run before ExportAsFixedFormat.
    """
    report = {}
    for ws in wb.Worksheets:
        ps = ws.PageSetup
        pages_before = ps.Pages.Count  # triggers Excel's real print layout calc

        if pages_before > 1:
            logger.warning(
                "Sheet '%s' overflows (%d pages, zoom=%s) — forcing fit-to-page",
                ws.Name, pages_before, ps.Zoom
            )
            ps.Zoom = False
            ps.FitToPagesWide = 1
            ps.FitToPagesTall = False
            pages_after = ps.Pages.Count
        else:
            pages_after = pages_before

        report[ws.Name] = {"before": pages_before, "after": pages_after}

    return report
# def check_and_fix_scaling(wb, logger) -> dict:
#     """Detect non-standard page scaling per sheet, force fit-to-page,
#     and return a report for observability/alerting."""
#     report = {}
#     for ws in wb.Worksheets:
#         ps = ws.PageSetup
#         original_zoom = ps.Zoom      # False if FitToPages is used, else % value
#         original_scale = ps.Zoom if ps.Zoom else None

#         is_default = (ps.Zoom == 100 or ps.Zoom is False and ps.FitToPagesWide == 1)
#         report[ws.Name] = {
#             "had_scale": original_scale,
#             "was_default_100": (original_scale == 100),
#         }
#         if original_scale == 100:
#             logger.warning("Sheet '%s' had no scaling set (100%% default) — forcing fit-to-page", ws.Name)

#         ps.Zoom = False
#         ps.FitToPagesWide = 1
#         ps.FitToPagesTall = False

#     return report
