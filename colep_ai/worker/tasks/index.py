"""
worker/tasks/index.py

Index task — runs embedding + Azure AI Search upsert for a single file's
ingestion results.

Input: source_file name (used to locate results_dir on disk).
       run_page_indexing() reads all page JSON files from results_dir —
       no data is passed through the Celery chain itself.

On success: updates file status to 'indexing_done', chain continues to cleanup.
On failure: updates file status to 'failed', raises to break chain.
            cleanup_task is dispatched via on_failure callback.
"""

from __future__ import annotations

from pathlib import Path

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.indexing.azure_page_pipeline import run_page_indexing
from colep_ai.indexing.embedder import get_openai_client_sync
from colep_ai.ingestion.utils import normalize_filename
from colep_ai.worker.celery_app import celery_app
from colep_ai.worker.tracker import update_file_status

logger = get_logger(__name__)


@celery_app.task(
    name="colep_ai.worker.tasks.index.index_task",
    bind=True,
    queue="indexing",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=2,
    default_retry_delay=30,
)
def index_task(self, job_id: str, filename: str, folder_name: str = "") -> None:
    """
    Embeds and upserts all page JSON results for a single file into
    Azure AI Search.

    Args:
        job_id:   UUID of the parent ingestion job.
        filename: Original .xlsx filename — used to derive source_file
                  and locate results_dir on disk.

    Note:
        run_page_indexing() reads from:
          outputs/{source_file}/results/*_result.json
        It does NOT receive data through the chain — it reads from disk.
        This is intentional: results can be large, passing them through
        Redis (Celery backend) would be wasteful.
    """
    logger.info(f"[index] job={job_id} file='{filename}' started")

    try:
        update_file_status(job_id, filename, "indexing")

        source_file = normalize_filename(Path(filename).stem)
        results_dir = settings.results_dir(source_file)

        if not results_dir.exists():
            raise RuntimeError(
                f"Results directory not found: {results_dir}. "
                f"Ingestion may not have completed successfully."
            )

        result_files = list(results_dir.glob("*_result.json"))
        if not result_files:
            logger.warning(f"[index] job={job_id} file='{filename}' no result JSONs — marking skipped")
            update_file_status(job_id, filename, "skipped", reason = "no result JSONs produced — all pages classified as skip by page classifier" )
            return

        logger.info(
            f"[index] job={job_id} file='{filename}' "
            f"indexing {len(result_files)} page result(s) from {results_dir}"
        )

        openai_client = get_openai_client_sync()
        run_page_indexing(str(results_dir), openai_client, folder_name=folder_name)

        update_file_status(job_id, filename, "indexing_done")
        logger.info(f"[index] job={job_id} file='{filename}' indexing complete")

    except Exception as exc:
        logger.exception(f"[index] job={job_id} file='{filename}' failed: {exc}")
        update_file_status(job_id, filename, "failed", error=str(exc))
        raise


# Cleanup on failure is handled by chain.on_error() in orchestrator.py.
# No on_failure hook needed here.