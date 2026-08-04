"""
worker/tasks/ingest.py

Ingest task — runs the full Excel → PDF → PNGs → per-page Claude pipeline
for a single .xlsx file.

Pipeline stages (per file):
  1. Excel → PDF via win32com (sequential — COM requirement)
  2. PDF → page PNGs via PyMuPDF (sequential — renders all pages at once)
  3. Per-page processing via ThreadPoolExecutor (parallel)
       - CV image crop extraction
       - Claude vision association → structured JSON
  4. Idempotency check: if all page JSONs already exist, skip to indexing

CRITICAL — win32com constraint:
  win32com (used inside excel_to_pdf via _ensure_pdf) requires CoInitialize()
  to be called on the thread that uses it. This task calls CoInitialize()
  at the top of the task function — BEFORE any pipeline call.

  DO NOT move excel_to_pdf() or _ensure_pdf() inside the ThreadPoolExecutor.
  COM must be initialized on the SAME thread that calls the COM object.
  The thread pool is only used AFTER PDF and PNG generation is complete.

  This constraint must be preserved if the pipeline is ever refactored.

Windows-only:
  This task must run on Windows workers because win32com is Windows-only.
  Indexing and cleanup tasks have no such constraint.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pythoncom

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.generation.claude_client import get_claude_client_sync
from colep_ai.ingestion.pipeline import get_total_pages, run_pipeline, _ensure_pdf, _ensure_page_image
from colep_ai.ingestion.utils import normalize_filename
from colep_ai.worker.celery_app import celery_app
from colep_ai.worker.tracker import update_file_status

logger = get_logger(__name__)


def _all_page_results_exist(source_file: str, total_pages: int) -> bool:
    """
    Idempotency check: returns True if all expected per-page JSON result
    files already exist on disk. If True, ingestion is skipped and the
    task proceeds directly to indexing.

    This prevents re-running the full pipeline on Celery requeue after
    a worker crash that occurred AFTER ingestion completed but BEFORE
    the task was acknowledged.
    """
    results_dir = settings.results_dir(source_file)
    for page in range(1, total_pages + 1):
        result_path = results_dir / f"{source_file}_page_{page}_result.json"
        if not result_path.exists():
            return False
    return True


def _process_page(
    excel_path: str,
    page_number: int,
    claude_client,
    folder_name: str = "",
) -> tuple[int, bool, str | None]:
    """
    Processes a single page. Runs inside a ThreadPoolExecutor thread.

    Returns:
        (page_number, success, error_message)

    Note: win32com is NOT called here. PDF and PNG files are guaranteed
    to exist before this function is called. Only Claude + CV crop
    extraction runs in threads — both are thread-safe.
    """
    try:
        run_pipeline(
            excel_path=excel_path,
            page_number=page_number,
            claude_client=claude_client,
             folder_name=folder_name,   
        )
        return page_number, True, None
    except Exception as exc:
        logger.error(
            f"[ingest] page {page_number} failed for {Path(excel_path).name}: {exc}",
            exc_info=True,
        )
        return page_number, False, str(exc)


@celery_app.task(
    name="colep_ai.worker.tasks.ingest.ingest_task",
    bind=True,
    queue="ingestion",
    # Reliability: requeue on worker crash
    acks_late=True,
    reject_on_worker_lost=True,
    # Retries: retry the full file on transient failures (network, API blip)
    # max_retries=1 — we don't want to hammer Claude API on repeated failures
    max_retries=1,
    default_retry_delay=60,
)
def ingest_task(self, job_id: str, local_path: str, filename: str, folder_name: str = "") -> None:
    """
    Runs the ingestion pipeline for a single .xlsx file.

    Args:
        job_id:     UUID of the parent ingestion job.
        local_path: Absolute path to the downloaded .xlsx file.
        filename:   Original filename (used as tracker key).

    On success: updates file status to 'ingesting_done'.
    On failure: updates file status to 'failed', raises to break the chain.

    Chain break on failure is intentional — index_task must not run on
    incomplete ingestion results.
    """
    logger.info(f"[ingest] job={job_id} file='{filename}' started")

    # ── CRITICAL: Initialize COM on this thread before any win32com call ──
    # excel_to_pdf() (called inside _ensure_pdf) uses win32com.client.DispatchEx.
    # CoInitialize must be called on the same thread that uses COM objects.
    # CoUninitialize is called in the finally block to clean up.
    # DO NOT remove this — silent COM crashes will result.
    pythoncom.CoInitialize()

    try:
        update_file_status(job_id, filename, "ingesting")

        excel_path = Path(local_path)
        source_file = normalize_filename(excel_path.stem)

        settings.ensure_doc_dirs(source_file)

        # ── Stage 1 & 2: Excel → PDF → PNGs (sequential, win32com) ───────
        # These must complete before threading starts.
        # _ensure_pdf uses file-existence caching — safe to call on requeue.
        # _ensure_page_image renders ALL pages in one pass — also cached.
        from colep_ai.ingestion.excel_to_image import pdf_to_images

        pdf_path = _ensure_pdf(excel_path, source_file,folder_name)
        pdf_to_images(
            str(pdf_path),
            str(settings.page_images_dir(source_file)),
            folder_name,
            source_file,
        )

        total_pages = get_total_pages(local_path)
        logger.info(f"[ingest] job={job_id} file='{filename}' total_pages={total_pages}")

        # ── Idempotency check ──────────────────────────────────────────────
        # If all page JSONs already exist, this is a requeue after a crash
        # that happened post-ingestion. Skip straight to done.
        if _all_page_results_exist(source_file, total_pages):
            logger.info(
                f"[ingest] job={job_id} file='{filename}' "
                f"all {total_pages} page results already exist — skipping ingestion"
            )
            update_file_status(job_id, filename, "ingesting_done")
            return

        # ── Stage 3: Per-page processing (parallel) ────────────────────────
        # Claude client is thread-safe — one instance shared across threads.
        # win32com is NOT used past this point — safe to thread.
        claude_client = get_claude_client_sync()

        failed_pages: list[int] = []
        succeeded_pages: list[int] = []

        with ThreadPoolExecutor(max_workers=settings.PAGE_THREAD_WORKERS) as executor:
            futures = {
                executor.submit(_process_page, local_path, page, claude_client, folder_name): page
                for page in range(1, total_pages + 1)
            }
            for future in as_completed(futures):
                page_number, success, error = future.result()
                if success:
                    succeeded_pages.append(page_number)
                else:
                    failed_pages.append(page_number)

        logger.info(
            f"[ingest] job={job_id} file='{filename}' "
            f"pages: succeeded={len(succeeded_pages)} failed={len(failed_pages)}"
        )

        # ── Page failure threshold check ───────────────────────────────────
        # INGEST_PAGE_FAILURE_THRESHOLD is a config constant (default 0.5).
        # If too many pages failed, mark file as failed and break the chain.
        # This prevents indexing partial results that are too incomplete.
        if total_pages > 0:
            failure_ratio = len(failed_pages) / total_pages
            if failure_ratio > settings.INGEST_PAGE_FAILURE_THRESHOLD:
                error_msg = (
                    f"{len(failed_pages)}/{total_pages} pages failed "
                    f"(ratio={failure_ratio:.2f} > threshold={settings.INGEST_PAGE_FAILURE_THRESHOLD})"
                )
                logger.error(f"[ingest] job={job_id} file='{filename}' {error_msg}")
                update_file_status(job_id, filename, "failed", error=error_msg)
                # Raise to break the chain — index_task will not run
                raise RuntimeError(error_msg)

        # ── Success ────────────────────────────────────────────────────────
        if failed_pages:
            logger.warning(
                f"[ingest] job={job_id} file='{filename}' "
                f"proceeding to indexing with {len(failed_pages)} failed pages: {failed_pages}"
            )

        update_file_status(job_id, filename, "ingesting_done")
        logger.info(f"[ingest] job={job_id} file='{filename}' ingestion complete")

    except Exception as exc:
        logger.exception(f"[ingest] job={job_id} file='{filename}' failed: {exc}")
        update_file_status(job_id, filename, "failed", error=str(exc))
        # Re-raise to break the Celery chain.
        # index_task and cleanup_task will not run automatically.
        # cleanup_task is linked via on_failure callback (see below) to
        # ensure downloaded .xlsx is still deleted and counters updated.
        raise

    finally:
        # Always uninitialize COM — even on exception
        pythoncom.CoUninitialize()


# Cleanup on failure is handled by chain.on_error() in orchestrator.py.
# No on_failure hook needed here.