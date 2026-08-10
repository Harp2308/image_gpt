"""
Excel -> PDF via win32com.
IRM sensitivity-label tags are stripped from a temp copy before Excel opens it,
eliminating the AAD sign-in dialog entirely.
"""

import json
import re
from pathlib import Path
import tempfile
import pymupdf as fitz
import win32com.client

from colep_ai.core.logger import get_logger
from colep_ai.ingestion.irm_strip import strip_irm
from colep_ai.utils.blob_storage import (
    get_blob_client,
    blob_pdf_key,
    blob_page_image_key,
)
logger = get_logger(__name__)

XL_PAGE_BREAK_MANUAL = -4135

# xlSheetHidden = 0 (hidden but user can unhide via UI)
# xlSheetVeryHidden = 2 (hidden, user cannot unhide via UI)
# xlSheetVisible = -1
XL_SHEET_VISIBLE = -1
XL_SHEET_HIDDEN = 0

# Matches sheets ending with exactly " (N)" — e.g. "Turno (2)", "Mensal (3)".
# Requires a space before the paren, digits inside, nothing after closing paren.
_NUMBERED_DUPLICATE_RE = re.compile(r' \(\d+\)$')

def _is_tab_colored(ws) -> bool:
    # xlColorIndexNone = -4142 — default, no color set
    return ws.Tab.ColorIndex != -4142

def _should_skip_sheet(ws) -> bool:
    """
    Returns True for sheets that are archived/duplicate and should be
    excluded from PDF export.

    Rules:
      1. Name contains "Old"      → archived version of an active sheet.
      2. Name ends with " (N)" AND tab has no color → obsolete duplicate.
         " (N)" sheets WITH a colored tab are Part 2 content — keep them.
    """
    name = ws.Name
    if "Old" in name:
        return True
    if _NUMBERED_DUPLICATE_RE.search(name):
        return not _is_tab_colored(ws)
    return False


def _has_manual_page_breaks(ws) -> bool:
    try:
        for brk in ws.HPageBreaks:
            if brk.Type == XL_PAGE_BREAK_MANUAL:
                return True
        for brk in ws.VPageBreaks:
            if brk.Type == XL_PAGE_BREAK_MANUAL:
                return True
        return False
    except Exception as e:
        logger.warning(f"_has_manual_page_breaks: COM error on sheet '{ws.Name}', assuming no manual breaks: {e}")
        return False

def normalize_sheet_pagination(wb, logger) -> dict:
    """
    Ensure every visible, non-manually-broken sheet exports as exactly
    one page per intended content block.

    Four cases handled:

      Case A — manual page breaks present:
        Sheet is deliberately multi-page (e.g. one lubrication block per
        printed page). Leave completely untouched. Forcing fit-to-page here
        discards the breaks and produces blank interleaved pages.

      Case B — fit-to-page already set, FitToPagesTall == 1:
        Sheet is correctly configured. Nothing to do.

      Case C — fit-to-page already set, FitToPagesTall != 1:
        Author set Zoom=False (fit mode) but left the tall axis unconstrained
        (False = unlimited) or set it to > 1. Excel's print engine absorbs
        this gracefully; ExportAsFixedFormat does not — it takes FitToPagesTall
        literally and bleeds a footer onto a second page.
        Fix: force FitToPagesTall = 1. FitToPagesWide is already 1, leave it.

      Case D — neither fit-to-page nor manual breaks (Zoom is a percentage):
        Sheet was never configured for print. If it overflows, force
        FitToPagesWide=1, FitToPagesTall=False (unconstrained tall is correct
        here because we want content scaled to fit width, height follows).

    Hidden sheets are skipped entirely — they are excluded from PDF export
    and the expensive ps.Pages.Count COM call is unnecessary for them.

    Returns the normalization report AND a page-count dict {sheet_name: page_count}
    for visible sheets, used to build the sheet→PDF-page map.
    """
    report = {}
    sheet_page_counts = {}  # sheet_name -> number of pages it contributes to the PDF

    for ws in wb.Worksheets:

        # Skip sheets hidden by _should_skip_sheet — they won't appear in
        # the PDF so there's nothing to normalize and no pages to map.
        if ws.Visible != XL_SHEET_VISIBLE:
            report[ws.Name] = {"action": "skipped", "reason": "sheet_hidden"}
            continue

        ps = ws.PageSetup
        ws.Calculate()

        has_manual_breaks = _has_manual_page_breaks(ws)

        # ── Case A: manual breaks — intentional multi-page layout ─────────
        if has_manual_breaks:
            # Pages.Count still needed for the sheet map — manual-break sheets
            # can span multiple PDF pages and we must account for all of them.
            page_count = ps.Pages.Count
            sheet_page_counts[ws.Name] = page_count
            report[ws.Name] = {
                "action": "skipped",
                "reason": "manual_breaks_present",
                "pages": page_count,
            }
            logger.info(f"Sheet '{ws.Name}': skipped normalization (manual_breaks_present, pages={page_count})")
            continue

        fit_to_page_already_set = (ps.Zoom is False)

        # ── Case B & C: fit-to-page mode is active ────────────────────────
        if fit_to_page_already_set:
            fit_wide = ps.FitToPagesWide
            fit_tall = ps.FitToPagesTall  # False = unconstrained, int = explicit page count

            # Case B: already correct — exactly 1 page tall
            if fit_tall == 1:
                sheet_page_counts[ws.Name] = 1
                report[ws.Name] = {
                    "action": "skipped",
                    "reason": "fit_to_page_already_set",
                    "FitToPagesWide": fit_wide,
                    "FitToPagesTall": fit_tall,
                    "pages": 1,
                }
                logger.info(
                    f"Sheet '{ws.Name}': skipped normalization "
                    f"(fit_to_page_already_set, wide={fit_wide}, tall={fit_tall})"
                )
                continue

            # Case C: fit mode active but tall axis is unconstrained or > 1.
            # ExportAsFixedFormat honours FitToPagesTall literally unlike the
            # print engine — force to 1 to prevent footer bleed onto page 2.
            logger.warning(
                f"Sheet '{ws.Name}': fit-to-page set but FitToPagesTall={fit_tall} "
                f"(wide={fit_wide}) — forcing FitToPagesTall=1 to prevent PDF bleed"
            )
            ps.FitToPagesTall = 1
            pages_after = ps.Pages.Count
            sheet_page_counts[ws.Name] = pages_after
            report[ws.Name] = {
                "action": "fixed_fit_tall",
                "FitToPagesWide": fit_wide,
                "FitToPagesTall_before": fit_tall,
                "FitToPagesTall_after": 1,
                "pages": pages_after,
            }
            continue

        # ── Case D: no fit-to-page, no manual breaks (Zoom = percentage) ──
        # Triggers the expensive Pages.Count call only when actually needed.
        pages_before = ps.Pages.Count

        if pages_before > 1:
            logger.warning(
                f"Sheet '{ws.Name}' overflows ({pages_before} pages, zoom={ps.Zoom}), "
                f"no manual breaks and no existing fit-to-page config -> forcing fit-to-page"
            )
            ps.Zoom = False
            ps.FitToPagesWide = 1
            ps.FitToPagesTall = False  # unconstrained tall: scale to fit width, height follows
            pages_after = ps.Pages.Count
            sheet_page_counts[ws.Name] = pages_after
            report[ws.Name] = {
                "action": "forced_fit",
                "before": pages_before,
                "after": pages_after,
                "pages": pages_after,
            }
        else:
            sheet_page_counts[ws.Name] = pages_before
            report[ws.Name] = {
                "action": "untouched",
                "before": pages_before,
                "after": pages_before,
                "pages": pages_before,
            }

    return report, sheet_page_counts


def _build_sheet_map(sheet_page_counts: dict) -> dict:
    """
    Convert {sheet_name: page_count} (ordered, visible sheets only) into
    {pdf_page_number: sheet_name} — 1-based, matching PDF page numbering.

    Example:
        {"Capa": 1, "Turno": 1, "Semanal": 1} ->
        {"1": "Capa", "2": "Turno", "3": "Semanal"}

    String keys are used so the JSON round-trips cleanly without int→str
    conversion surprises.
    """
    sheet_map = {}
    page_counter = 1
    for sheet_name, count in sheet_page_counts.items():
        for _ in range(count):
            sheet_map[str(page_counter)] = sheet_name
            page_counter += 1
    return sheet_map

# in excel_to_pdf, wrap the whole thing:
TRANSIENT_COM_ERRORS = {
    -2147418111,  # RPC_E_CALL_FAILED — Excel busy/timeout
    -2147023174,  # RPC_S_SERVER_UNAVAILABLE
}

STRUCTURAL_COM_ERRORS = {
    -2147352565,  # E_FAIL — Excel internal exception (corrupt file, bad refs)
    -2147024893,  # PATH_NOT_FOUND
    -2147024894,  # FILE_NOT_FOUND
}

def excel_to_pdf(excel_path: str, pdf_dir: str, source_file: str, folder_name: str) -> str:
    pdf_dir_p = Path(pdf_dir).resolve()
    pdf_dir_p.mkdir(parents=True, exist_ok=True)
    pdf_path = str(pdf_dir_p / f"{source_file}.pdf")
    sheet_map_path = pdf_dir_p / f"{source_file}.sheet_map.json"

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
                UpdateLinks=False,
                ReadOnly=True,
                IgnoreReadOnlyRecommended=True,
            )
            logger.info(f"[excel_to_pdf] STAGE:open OK | {source_file}")

            try:
                if wb.MultiUserEditing:
                    wb.ExclusiveAccess()
            except Exception:
                pass

            try:
                excel.CalculateFull()
                logger.info(f"[excel_to_pdf] STAGE:calculate OK | {source_file}")
            except Exception as e:
                logger.warning(f"[excel_to_pdf] STAGE:calculate FAILED (non-fatal) | {source_file}: {e}")

            hidden_sheets = []
            for ws in wb.Worksheets:
                if _should_skip_sheet(ws):
                    ws.Visible = XL_SHEET_HIDDEN
                    hidden_sheets.append(ws.Name)
            if hidden_sheets:
                logger.info(f"[excel_to_pdf] STAGE:hide_sheets | {source_file}: {hidden_sheets}")

            logger.info(f"[excel_to_pdf] STAGE:normalize_start | {source_file}")
            report, sheet_page_counts = normalize_sheet_pagination(wb, logger)
            logger.info(f"[excel_to_pdf] STAGE:normalize OK | {source_file}: {report}")

            sheet_map = _build_sheet_map(sheet_page_counts)
            sheet_map_path.write_text(
                json.dumps(sheet_map, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"[excel_to_pdf] STAGE:sheet_map OK | {source_file}: {sheet_map}")

            try:
                logger.info(f"[excel_to_pdf] STAGE:export_start | {source_file}")
                wb.ExportAsFixedFormat(0, pdf_path)
                logger.info(f"[excel_to_pdf] STAGE:export OK | {source_file}")
            finally:
                wb.Close(False)
        finally:
            excel.Quit()
        # try:
        #     wb = excel.Workbooks.Open(
        #         stripped_path,
        #         UpdateLinks=False,   # don't prompt to update external links
        #         ReadOnly=True,       # read-only avoids shared-workbook Group mode
        #         IgnoreReadOnlyRecommended=True,
        #     )

        #     # Force out of shared/group mode if still set — this is what causes
        #     # the "Group" title bar and forces a visible window
        #     try:
        #         if wb.MultiUserEditing:
        #             wb.ExclusiveAccess()
        #     except Exception:
        #         pass  # not all workbooks support this; safe to ignore

        #     # ── Hide Old/duplicate sheets before export ──────────────────────
        #     # ExportAsFixedFormat skips hidden sheets natively.
        #     # We hide in-memory only; wb.Close(False) ensures nothing is saved back.
        #     # Must run BEFORE normalize_sheet_pagination so hidden sheets are
        #     # skipped from the normalization loop and excluded from the sheet map.
        #     hidden_sheets = []
        #     for ws in wb.Worksheets:
        #         if _should_skip_sheet(ws):
        #             ws.Visible = XL_SHEET_HIDDEN
        #             hidden_sheets.append(ws.Name)
        #     if hidden_sheets:
        #         logger.info(f"Sheets hidden from PDF export for {source_file}: {hidden_sheets}")
        #     # ────────────────────────────────────────────────────────────────

        #     report, sheet_page_counts = normalize_sheet_pagination(wb, logger)
        #     logger.info(f"pagination normalization report for {source_file}: {report}")

        #     # ── Build and persist sheet→PDF-page map ─────────────────────────
        #     # Built while wb is open — only opportunity to get sheet names and
        #     # their page counts. Persisted as a sidecar JSON next to the PDF.
        #     # Same lifecycle as the PDF cache: if PDF exists, map exists.
        #     sheet_map = _build_sheet_map(sheet_page_counts)
        #     sheet_map_path.write_text(
        #         json.dumps(sheet_map, ensure_ascii=False, indent=2),
        #         encoding="utf-8",
        #     )
        #     logger.info(f"Sheet map written: {sheet_map_path} | {sheet_map}")
        #     # ────────────────────────────────────────────────────────────────

        #     try:
        #         wb.ExportAsFixedFormat(0, pdf_path)
        #     finally:
        #         wb.Close(False)
        # finally:
        #     excel.Quit()

    if not Path(pdf_path).exists():
        raise RuntimeError(f"Excel export failed, no PDF at {pdf_path}")

    logger.info(f"excel_to_pdf: {excel_path} -> {pdf_path}")
    # ── Blob upload (after local save — local copy untouched) ──
    blob_key = blob_pdf_key(folder_name, source_file, f"{source_file}.pdf")
    get_blob_client().upload_file(pdf_path, blob_key)
    return pdf_path


def word_to_pdf(word_path: str, pdf_dir: str, source_file: str, folder_name: str) -> str:
    pdf_dir_p = Path(pdf_dir).resolve()
    pdf_dir_p.mkdir(parents=True, exist_ok=True)
    pdf_path = str(pdf_dir_p / f"{source_file}.pdf")

    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = False

    try:
        doc = word.Documents.Open(
            str(Path(word_path).resolve()),
            ReadOnly=True,
            AddToRecentFiles=False,
        )
        try:
            doc.SaveAs(pdf_path, FileFormat=17)  # 17 = wdFormatPDF
        finally:
            doc.Close(False)
    finally:
        word.Quit()

    if not Path(pdf_path).exists():
        raise RuntimeError(f"Word export failed, no PDF at {pdf_path}")

    logger.info(f"word_to_pdf: {word_path} -> {pdf_path}")

    # ── Blob upload (after local save — local copy untouched) ──
    blob_key = blob_pdf_key(folder_name, source_file, f"{source_file}.pdf")
    get_blob_client().upload_file(pdf_path, blob_key)
    return pdf_path


def pdf_to_images(pdf_path: str, output_dir: str, folder_name: str, source_file: str, dpi: int = 200) -> list[str]:
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
            # ── Blob upload per page (after local save) ──
            blob_key = blob_page_image_key(folder_name, source_file, img_path.name)
            get_blob_client().upload_file(img_path, blob_key)

    finally:
        doc.close()

    logger.info(f"pdf_to_images: {pdf_path} -> {len(image_paths)} pages")
    return image_paths

