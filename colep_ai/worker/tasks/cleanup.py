"""
worker/tasks/cleanup.py

Cleanup task — final stage of every file's chain regardless of outcome.

Responsibilities:
  1. Delete the downloaded .xlsx file (outputs are never deleted)
  2. Update file status to 'done' or keep 'failed' (if upstream failed)
  3. Increment the appropriate Redis counter (done or failed)
  4. Trigger job completion check via tracker

This task is ALWAYS dispatched — either:
  a) At the end of the successful chain: ingest → index → cleanup
  b) Via on_failure callbacks from ingest_task or index_task when
     the chain breaks due to an upstream failure

The ingestion_failed / indexing_failed kwargs distinguish these cases
so the tracker records the correct final status.
"""

from __future__ import annotations

from pathlib import Path

from colep_ai.core.logger import get_logger
from colep_ai.worker.celery_app import celery_app
from colep_ai.worker.tracker import (
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
        local_path:        Path to the downloaded .xlsx to delete.
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
        # ── Step 1: Delete downloaded .xlsx ───────────────────────────────
        # Outputs (PDF, PNGs, JSONs, combined images) are never deleted.
        # Only the raw downloaded Excel is removed.
        xlsx_path = Path(local_path)
        if xlsx_path.exists():
            try:
                xlsx_path.unlink()
                logger.info(f"[cleanup] deleted {xlsx_path}")
            except Exception as exc:
                # Log but don't fail — missing file is not critical
                logger.warning(f"[cleanup] could not delete {xlsx_path}: {exc}")
        else:
            logger.info(f"[cleanup] {xlsx_path} already gone — skipping delete")

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
            update_file_status(job_id, filename, "done")
            record_file_done(job_id)
            logger.info(
                f"[cleanup] job={job_id} file='{filename}' recorded as done"
            )

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