"""
worker/tasks/orchestrator.py

Handles 3 ingestion modes:
  - sharepoint_folder : download all .xlsx from a SP folder
  - sharepoint_files  : download specific files by SP path (per-file folder_name)
  - local             : files already saved by API route (one folder_name for batch)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from celery import chain, group

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.sharepoint.client import SharePointClient
from colep_ai.worker.celery_app import celery_app
from colep_ai.worker.tracker import (
    create_file_record,
    record_file_failed,
    update_file_status,
    update_job_status,
)
from colep_ai.worker.tasks.cleanup import cleanup_task
from colep_ai.worker.tasks.index import index_task
from colep_ai.worker.tasks.ingest import ingest_task

logger = get_logger(__name__)


def _extract_folder_name(file_path: str) -> str:
    """
    /ShopFloor/Line5/O01.xlsx  →  'Line5'
    Falls back to empty string if path has fewer than 2 segments.
    """
    parts = [p for p in file_path.split("/") if p]
    return parts[-2] if len(parts) >= 2 else ""


def _dispatch_chains(job_id: str, files: list[dict]) -> None:
    """
    Fan out one chain per file.
    Each file dict must have: name, local_path, folder_name
    """
    chains = []
    for f in files:
        filename = f["name"]
        local_path = f["local_path"]
        folder_name = f["folder_name"]

        create_file_record(job_id, filename, folder_name)

        file_chain = chain(
            ingest_task.si(job_id, local_path, filename, folder_name),
            index_task.si(job_id, filename, folder_name),
            cleanup_task.si(job_id, local_path, filename),
        ).on_error(
            cleanup_task.si(job_id, local_path, filename, ingestion_failed=True)
        )
        chains.append(file_chain)

    if chains:
        group(*chains).apply_async()
        logger.info(f"[orchestrator] job={job_id} dispatched {len(chains)} chains")
    else:
        logger.error(f"[orchestrator] job={job_id} no chains dispatched")


@celery_app.task(
    name="colep_ai.worker.tasks.orchestrator.orchestrator_task",
    bind=True,
    queue="orchestration",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=2,
    default_retry_delay=30,
)
def orchestrator_task(
    self,
    job_id: str,
    payload: str | list,
    mode: str,
    folder_name: str = "",   # only used by local mode
) -> None:
    logger.info(f"[orchestrator] job={job_id} mode={mode} started")
    local_dir = settings.downloads_dir(job_id)

    try:
        update_job_status(job_id, "running")

        # ── Mode: sharepoint_folder ────────────────────────────────────────
        if mode == "sharepoint_folder":
            sp_client = SharePointClient()
            folder_path: str = payload

            try:
                downloaded = sp_client.download_folder(folder_path, local_dir)
            except Exception as exc:
                logger.exception(f"[orchestrator] job={job_id} SP folder download failed: {exc}")
                update_job_status(job_id, "failed")
                if local_dir.exists():
                    shutil.rmtree(local_dir, ignore_errors=True)
                return

            if not downloaded:
                logger.error(f"[orchestrator] job={job_id} no files downloaded")
                update_job_status(job_id, "failed")
                return

            # Account for files that failed to download
            sp_total = len(sp_client.list_xlsx_files(folder_path))
            downloaded_names = {f["name"] for f in downloaded}
            all_names = {f["name"] for f in sp_client.list_xlsx_files(folder_path)}

            update_job_status(job_id, "running", total_files=sp_total)

            # Pre-mark failed downloads
            for name in (all_names - downloaded_names):
                create_file_record(job_id, name, "")
                update_file_status(job_id, name, "failed", error="SharePoint download failed")
                record_file_failed(job_id)

            files = [
                {
                    "name": f["name"],
                    "local_path": f["local_path"],
                    "folder_name": _extract_folder_name(f["sharepoint_path"]),
                }
                for f in downloaded
            ]
            _dispatch_chains(job_id, files)

        # ── Mode: sharepoint_files ─────────────────────────────────────────
        elif mode == "sharepoint_files":
            sp_client = SharePointClient()
            file_paths: list[str] = payload

            downloaded = []
            failed = []
            for file_path in file_paths:
                try:
                    result = sp_client.download_file_by_path(file_path, local_dir)
                    result["folder_name"] = _extract_folder_name(file_path)
                    downloaded.append(result)
                except Exception as exc:
                    logger.exception(f"[orchestrator] job={job_id} failed to download '{file_path}': {exc}")
                    failed.append(Path(file_path).name)

            total = len(file_paths)
            update_job_status(job_id, "running", total_files=total)

            # Pre-mark failed downloads
            for name in failed:
                create_file_record(job_id, name, "")
                update_file_status(job_id, name, "failed", error="SharePoint download failed")
                record_file_failed(job_id)

            if not downloaded:
                update_job_status(job_id, "failed")
                return

            _dispatch_chains(job_id, downloaded)

        # ── Mode: local ────────────────────────────────────────────────────
        elif mode == "local":
            saved_files: list[dict] = payload  # [{name, local_path}, ...]
            update_job_status(job_id, "running", total_files=len(saved_files))

            files = [
                {
                    "name": f["name"],
                    "local_path": f["local_path"],
                    "folder_name": folder_name,
                }
                for f in saved_files
            ]
            _dispatch_chains(job_id, files)

        else:
            logger.error(f"[orchestrator] job={job_id} unknown mode='{mode}'")
            update_job_status(job_id, "failed")

    except Exception as exc:
        logger.exception(f"[orchestrator] job={job_id} unexpected error: {exc}")
        update_job_status(job_id, "failed")
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)
        raise