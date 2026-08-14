"""
worker/tasks/cleanup.py

Cleanup task — final stage of every file's chain regardless of outcome.

Responsibilities:
  1. Delete the downloaded .xlsx file ONLY on success (failed files kept for retry)
  2. Update file status to 'done' or keep 'failed' (if upstream failed)
  3. Increment the appropriate Redis counter (done or failed)
  4. Check if all files are terminal — if so, dispatch one-time retry for failed files

This task is ALWAYS dispatched — either:
  a) At the end of the successful chain: ingest → index → cleanup
  b) Via on_failure callbacks from ingest_task or index_task when
     the chain breaks due to an upstream failure

The ingestion_failed / indexing_failed kwargs distinguish these cases
so the tracker records the correct final status.
Retry logic:
  After every file completes, check_and_get_retry_files() checks whether
  all files in the job are terminal and there are failed files to retry.
  If so, mark_retry_dispatched() is called immediately (before dispatch)
  to prevent concurrent cleanup_task workers from double-dispatching,
  then failed files are re-queued through the full ingest -> index -> cleanup
  chain exactly once. Failed .xlsx files are kept on disk until this retry
  completes (or fails again).
"""

from __future__ import annotations

from pathlib import Path
from celery import chain

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.worker.celery_app import celery_app
from colep_ai.worker.tracker import (
    get_file_status, 
    check_and_get_retry_files,
    mark_retry_dispatched,
    record_file_done,
    record_file_failed,
    update_file_status,
)

logger = get_logger(__name__)


@celery_app.task(
    name="colep_ai.worker.tasks.cleanup.cleanup_task",
    bind=True,
    queue="cleanup",
    acks_late=True,
    reject_on_worker_lost=True,
    # Cleanup is trivial — 3 retries on transient Redis/disk failures
    max_retries=3,
    default_retry_delay=10,
)
def cleanup_task(
    self,
    job_id: str,
    local_path: str,
    filename: str,
    ingestion_failed: bool = False,
    indexing_failed: bool = False,
) -> None:
    """
    Cleans up after a file's pipeline chain completes (successfully or not).

    Args:
        job_id:            UUID of the parent ingestion job.
        local_path:        Path to the downloaded .xlsx to delete on success.
        filename:          Original filename (tracker key).
        ingestion_failed:  True if dispatched from ingest_task.on_failure.
        indexing_failed:   True if dispatched from index_task.on_failure.
    """
    upstream_failed = ingestion_failed or indexing_failed
    logger.info(
        f"[cleanup] job={job_id} file='{filename}' "
        f"upstream_failed={upstream_failed} started"
    )

    try:
        xlsx_path = Path(local_path)
         # ── Step 1: Delete .xlsx only on success ──────────────────────────
        # Failed files are kept on disk so the one-time retry can re-run
        # the full pipeline without needing to re-download from SharePoint.
        # The retry's cleanup_task will delete the file after the retry
        # attempt completes (success or final failure).
        if not upstream_failed:
            if xlsx_path.exists():
                try:
                    xlsx_path.unlink()
                    logger.info(f"[cleanup] deleted {xlsx_path}")
                except Exception as exc:
                    logger.warning(f"[cleanup] could not delete {xlsx_path}: {exc}")
            else:
                logger.info(f"[cleanup] {xlsx_path} already gone — skipping delete")
        else:
            logger.info(
                f"[cleanup] job={job_id} file='{filename}' "
                f"keeping {xlsx_path} on disk — reserved for retry"
                )

        # ── Step 2: Update file status + job completion counters ──────────
        if upstream_failed:
            # Status was already set to 'failed' by ingest_task or index_task.
            # Just increment the failed counter and check job completion.
            record_file_failed(job_id)
            logger.info(
                f"[cleanup] job={job_id} file='{filename}' recorded as failed"
            )
        else:
            # Full chain succeeded — mark file done and increment done counter
            current_status = get_file_status(job_id, filename)
            if current_status != "skipped":
                update_file_status(job_id, filename, "done")
            record_file_done(job_id)
            logger.info(
                f"[cleanup] job={job_id} file='{filename}' recorded as done"
            )
        # ── Step 3: Check if one-time retry should be dispatched ──────────
        # check_and_get_retry_files returns failed file records only when:
        #   - All files in the job are terminal (done/failed/skipped)
        #   - Job is not 'completed'
        #   - Retry has not already been dispatched
        # Returns None otherwise — nothing to do.
        _maybe_dispatch_retry(job_id)


    except Exception as exc:
        logger.exception(f"[cleanup] job={job_id} file='{filename}' error: {exc}")
        try:
            self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            # Cleanup itself failed after all retries.
            # Force-record as failed so job completion counter doesn't hang.
            logger.error(
                f"[cleanup] job={job_id} file='{filename}' "
                f"max retries exceeded — force-recording as failed"
            )
            try:
                record_file_failed(job_id)
            except Exception:
                logger.exception(
                    f"[cleanup] job={job_id} file='{filename}' "
                    f"could not record failure — job completion may hang"
                )



def _maybe_dispatch_retry(job_id: str) -> None:
    """
    Check retry conditions and dispatch one-time retry for failed files if met.

    Imports ingest_task and index_task inline to avoid circular imports
    (cleanup -> tasks -> cleanup).

    Race condition handling:
      mark_retry_dispatched() is called BEFORE dispatching chains.
      If two concurrent cleanup_task workers both reach this point
      simultaneously, check_and_get_retry_files() will return None for the
      second one because the flag is already set by the first.
      There is a narrow window between check and mark — acceptable given
      cleanup_task workers are not expected to finish within microseconds
      of each other for the same job.
    """
    failed_files = check_and_get_retry_files(job_id)
    if not failed_files:
        return

    # Import here to avoid circular import: cleanup -> ingest/index -> cleanup
    from colep_ai.worker.tasks.ingest import ingest_task
    from colep_ai.worker.tasks.index import index_task

    # Set flag BEFORE dispatching — prevents double-dispatch on race
    mark_retry_dispatched(job_id)

    dispatched = 0
    for file_rec in failed_files:
        filename = file_rec["filename"]
        folder_name = file_rec.get("folder_name", "")

        # Reconstruct local_path — file is guaranteed on disk (not deleted on failure)
        local_path = str(settings.downloads_dir(job_id) / filename)

        if not Path(local_path).exists():
            logger.warning(
                f"[cleanup] retry: '{filename}' not found on disk at {local_path} "
                f"— skipping this file"
            )
            continue

        # Reset file status to pending so tracker shows it's being retried
        update_file_status(job_id, filename, "pending")

        chain(
            ingest_task.si(job_id, local_path, filename, folder_name),
            index_task.si(job_id, filename, folder_name),
            cleanup_task.si(job_id, local_path, filename),
        ).on_error(
            cleanup_task.si(job_id, local_path, filename, ingestion_failed=True)
        ).apply_async()

        dispatched += 1
        logger.info(
            f"[cleanup] retry dispatched for '{filename}' | job={job_id}"
        )

    logger.info(
        f"[cleanup] job={job_id} retry pass: "
        f"dispatched={dispatched} skipped={len(failed_files) - dispatched}"
    )
