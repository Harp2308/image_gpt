"""
api/schemas/ingest.py

Pydantic request and response models for the ingestion API endpoints.
"""


from __future__ import annotations
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class SharePointFolderRequest(BaseModel):
    mode: Literal["sharepoint_folder"] = "sharepoint_folder"
    sharepoint_folder_paths: list[str] = Field(..., examples=[["/ShopFloor/Line5", "/ShopFloor/Line8"]])


class SharePointFilesRequest(BaseModel):
    mode: Literal["sharepoint_files"] = "sharepoint_files"
    sharepoint_file_paths: list[str] = Field(..., examples=[["/ShopFloor/Line5/O01.xlsx"]])


IngestStartRequest = Annotated[
    Union[SharePointFolderRequest, SharePointFilesRequest],
    Field(discriminator="mode"),
]

# ---------------------------------------------------------------------------
# Response — POST /ingest/start
# ---------------------------------------------------------------------------

class FolderJobResult(BaseModel):
    folder_path: str = Field(..., description="SharePoint folder path.")
    status: Literal["started", "skipped"] = Field(..., description="Whether a new job was created or skipped due to an active conflict.")
    job_id: str | None = Field(None, description="New job UUID if started.")
    existing_job_id: str | None = Field(None, description="Existing active job UUID if skipped.")


class IngestStartResponse(BaseModel):
    results: list[FolderJobResult]
    started: int = Field(..., description="Number of folders successfully queued.")
    skipped: int = Field(..., description="Number of folders skipped due to active job conflict.")


# ---------------------------------------------------------------------------
# Response — GET /ingest/status/{job_id}
# ---------------------------------------------------------------------------

JobStatus = Literal["pending", "running", "completed", "partial", "failed"]
FileStatus = Literal[
    "pending",
    "ingesting",
    "ingesting_done",
    "indexing",
    "indexing_done",
    "done",
    "skipped",
    "failed",
]


class FileStatusRecord(BaseModel):
    filename: str
    folder_name: str = ""
    status: FileStatus
    error: str | None = None
    reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class IngestStatusResponse(BaseModel):
    job_id: str
    sharepoint_folder_path: str
    status: JobStatus
    total_files: int
    completed_files: int
    failed_files: int
    started_at: str | None = None
    completed_at: str | None = None
    files: list[FileStatusRecord] = Field(
        default_factory=list,
        description="Per-file status records.",
    )


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------

class ConflictResponse(BaseModel):
    detail: str
    existing_job_id: str