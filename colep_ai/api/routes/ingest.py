from __future__ import annotations

import shutil
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from colep_ai.api.schemas.ingest import (
    ConflictResponse, FileStatusRecord, FolderJobResult, IngestStartRequest,
    IngestStartResponse, IngestStatusResponse,
)
from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger
from colep_ai.database.azure_search_client import delete_by_folder_name, get_search_client
from colep_ai.worker.tasks.orchestrator import orchestrator_task
from colep_ai.worker.tracker import (
    create_job, find_active_job_by_path,
    get_all_file_records, get_job,
)
from urllib.parse import unquote
logger = get_logger(__name__)
router = APIRouter(prefix="/ingest", tags=["Ingestion"])

ALLOWED_EXTENSIONS = (".xlsx", ".docx", ".doc")
@router.post("/start/sharepoint", status_code=202)
async def start_ingestion(request: IngestStartRequest):
    if request.mode == "sharepoint_folder":
        results: list[FolderJobResult] = []

        for raw_path in request.sharepoint_folder_paths:
            folder_path = unquote(raw_path.strip())
            existing = find_active_job_by_path(folder_path)
            if existing:
                results.append(FolderJobResult(
                    folder_path=folder_path,
                    status="skipped",
                    existing_job_id=existing,
                ))
                continue

            job_id = str(uuid.uuid4())
            create_job(job_id, folder_path)
            orchestrator_task.apply_async(
                args=[job_id, folder_path, "sharepoint_folder"],
                queue="orchestration",
            )
            results.append(FolderJobResult(
                folder_path=folder_path,
                status="started",
                job_id=job_id,
            ))

        return IngestStartResponse(
            results=results,
            started=sum(1 for r in results if r.status == "started"),
            skipped=sum(1 for r in results if r.status == "skipped"),
        )

    elif request.mode == "sharepoint_files":
        file_paths = [unquote(p.strip()) for p in request.sharepoint_file_paths]  # ← add
        job_id = str(uuid.uuid4())
        create_job(job_id, "")
        orchestrator_task.apply_async(
            args=[job_id, file_paths, "sharepoint_files"],
            queue="orchestration",
        )
        return {"job_id": job_id,  "message": "Job started.", "file_count": len(file_paths)}
        # return{"url":folder_path}

@router.post("/start/local", status_code=202)
async def start_local(
    folder_name: str = Form(...),
    files: list[UploadFile] = File(...),
):
    job_id = str(uuid.uuid4())
    local_dir = settings.downloads_dir(job_id)
    local_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for f in files:
        if not f.filename.lower().endswith(ALLOWED_EXTENSIONS):
            continue
        dest = local_dir / f.filename
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append({"name": f.filename, "local_path": str(dest)})

    if not saved:
        raise HTTPException(400, detail="No valid .xlsx files uploaded.")

    create_job(job_id, "")
    orchestrator_task.apply_async(
        args=[job_id, saved, "local", folder_name],
        queue="orchestration",
    )
    return {"job_id": job_id, "message": "Job started.", "file_count": len(saved)}


@router.delete("/folder/{folder_name}")
async def delete_folder(folder_name: str):
    client = get_search_client()
    deleted = delete_by_folder_name(client, folder_name)
    return {"folder_name": folder_name, "deleted_chunks": deleted}


@router.get("/status/{job_id}", response_model=IngestStatusResponse)
async def get_ingestion_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, detail=f"Job '{job_id}' not found.")
    file_records = get_all_file_records(job_id)
    files = [
        FileStatusRecord(
            filename=rec["filename"],
            folder_name=rec.get("folder_name", ""), 
            status=rec["status"],
            error=rec.get("error"),
            reason=rec.get("reason"),
            started_at=rec.get("started_at"),
            completed_at=rec.get("completed_at"),
        )
        for rec in sorted(file_records, key=lambda r: r["filename"])
    ]
    return IngestStatusResponse(
        job_id=job["job_id"],
        sharepoint_folder_path=job.get("sharepoint_folder_path", ""),
        status=job["status"],
        total_files=job["total_files"],
        completed_files=job["completed_files"],
        failed_files=job["failed_files"],
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        files=files,
    )