"""
Excel -> PDF via win32com.
IRM sensitivity-label tags are stripped from a temp copy before Excel opens it,
eliminating the AAD sign-in dialog entirely.
"""

from pathlib import Path
import tempfile
import fitz
import win32com.client

from colep_ai.core.logger import get_logger
from colep_ai.ingestion.irm_strip import strip_irm

logger = get_logger(__name__)

XL_PAGE_BREAK_MANUAL = -4135


def _has_manual_page_breaks(ws) -> bool:
    """
    True if a human explicitly placed a row or column page break on this
    sheet (PageBreak.Type == xlPageBreakManual). This is the strongest
    available signal that a sheet's multi-page layout is intentional
    (e.g. one lubrication-point photo block per printed page), as opposed
    to accidental overflow from an unconfigured print scale.
    """
    for brk in ws.HPageBreaks:
        if brk.Type == XL_PAGE_BREAK_MANUAL:
            return True
    for brk in ws.VPageBreaks:
        if brk.Type == XL_PAGE_BREAK_MANUAL:
            return True
    return False


def normalize_sheet_pagination(wb, logger) -> dict:
    """
    Force fit-to-page ONLY on sheets that both:
      (a) have no existing fit-to-page configuration, and
      (b) have no manual (human-placed) page breaks.

    Those two signals distinguish two structurally different situations
    that both present as "Pages.Count > 1", and require opposite handling:

      1. Sheet left at default scale (Zoom=100), nobody ever configured
         print layout, content simply overflows into 3-5 pages by
         accident. -> This IS a defect. Force fit-to-1-page-wide.

      2. Sheet deliberately authored as a multi-page document: manual
         page breaks + a fixed print scale, each page showing one real
         content block (e.g. legacy "Old" sheets with one lubrication
         point per printed page, separated by intentional blank spacer
         rows). -> This is NOT a defect. Forcing fit-to-page here
         discards the manual breaks, makes Excel recompute page
         boundaries from scratch, and desyncs them from the sparse
         content -> blank pages interleaved with content pages.

    Sheets matching (a) or (b) are left completely untouched, and we
    skip the expensive ps.Pages.Count call for them entirely (it forces
    Excel to run its real print-layout calculation).
    """
    report = {}
    for ws in wb.Worksheets:
        ps = ws.PageSetup

        fit_to_page_already_set = (ps.Zoom is False)
        has_manual_breaks = _has_manual_page_breaks(ws)

        if fit_to_page_already_set or has_manual_breaks:
            reason = (
                "fit_to_page_already_set"
                if fit_to_page_already_set
                else "manual_breaks_present"
            )
            report[ws.Name] = {"action": "skipped", "reason": reason}
            logger.info(f"Sheet '{ws.Name}': skipped normalization ({reason})")
            continue

        # Only sheets with neither signal reach here — candidates for the
        # "unconfigured, accidentally overflowing" correction.
        pages_before = ps.Pages.Count  # triggers Excel's real print layout calc

        if pages_before > 1:
            logger.warning(
                f"Sheet '{ws.Name}' overflows ({pages_before} pages, zoom={ps.Zoom}), "
                f"no manual breaks and no existing fit-to-page config -> forcing fit-to-page"
            )
            ps.Zoom = False
            ps.FitToPagesWide = 1
            ps.FitToPagesTall = False
            pages_after = ps.Pages.Count
            report[ws.Name] = {
                "action": "forced_fit",
                "before": pages_before,
                "after": pages_after,
            }
        else:
            report[ws.Name] = {
                "action": "untouched",
                "before": pages_before,
                "after": pages_before,
            }

    return report


def excel_to_pdf(excel_path: str, pdf_dir: str, source_file: str) -> str:
    pdf_dir_p = Path(pdf_dir).resolve()
    pdf_dir_p.mkdir(parents=True, exist_ok=True)
    pdf_path = str(pdf_dir_p / f"{source_file}.pdf")

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Strip IRM into a temp copy named after source_file — original never touched
        stripped_path = strip_irm(excel_path, str(Path(tmp_dir) / f"{source_file}.xlsx"))
        stripped_path = str(Path(stripped_path).resolve())

        excel = win32com.client.DispatchEx("Excel.Application")
        # Set ALL visibility/alert flags BEFORE opening any workbook
        excel.Visible = False
        excel.DisplayAlerts = False
        excel.ScreenUpdating = False   # prevents any UI redraws
        excel.EnableEvents = False     # suppresses event-driven dialogs

        try:
            wb = excel.Workbooks.Open(
                stripped_path,
                UpdateLinks=False,   # don't prompt to update external links
                ReadOnly=True,       # read-only avoids shared-workbook Group mode
                IgnoreReadOnlyRecommended=True,
            )

            # Force out of shared/group mode if still set — this is what causes
            # the "Group" title bar and forces a visible window
            try:
                if wb.MultiUserEditing:
                    wb.ExclusiveAccess()
            except Exception:
                pass  # not all workbooks support this; safe to ignore

            report = normalize_sheet_pagination(wb, logger)
            logger.info(f"pagination normalization report for {source_file}: {report}")
            try:
                wb.ExportAsFixedFormat(0, pdf_path)
            finally:
                wb.Close(False)
        finally:
            excel.Quit()

    if not Path(pdf_path).exists():
        raise RuntimeError(f"Excel export failed, no PDF at {pdf_path}")

    logger.info(f"excel_to_pdf: {excel_path} -> {pdf_path}")
    return pdf_path


def pdf_to_images(pdf_path: str, output_dir: str, source_file: str, dpi: int = 200) -> list[str]:
    output_dir_p = Path(output_dir)
    output_dir_p.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    image_paths = []
    try:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix)
            img_path = output_dir_p / f"{source_file}_page_{i}.png"
            pix.save(str(img_path))
            image_paths.append(str(img_path))
    finally:
        doc.close()

    logger.info(f"pdf_to_images: {pdf_path} -> {len(image_paths)} pages")
    return image_paths