"""
api/routes/blob_viewer.py
--------------------------
Endpoints to view images and files stored in Azure Blob Storage.
No download, no public access — streams directly from blob to browser.

Usage:
    GET /blob/view/{blob_key:path}

Example:
    /blob/view/source_file_abc/crops/page_1/crops/page_1_a3f2.png
    /blob/view/source_file_abc/pdf/source_file_abc.pdf
    /blob/view/source_file_abc/combined/page_1/page_1_step1_uid.png
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse
import io

from colep_ai.utils.blob_storage import get_blob_client
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/blob", tags=["Blob Viewer"])

_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".json": "application/json",
}


@router.get("/view/{blob_key:path}")
async def view_blob(blob_key: str):
    """
    Stream a blob directly to the browser.
    blob_key is the full path inside the container.
    e.g. /blob/view/source_file_abc/crops/page_1/crops/page_1_a3f2.png
    """
    suffix = f".{blob_key.rsplit('.', 1)[-1].lower()}" if "." in blob_key else ""
    content_type = _MIME_MAP.get(suffix, "application/octet-stream")

    try:
        container_client = get_blob_client()._container_client
        blob_client = container_client.get_blob_client(blob_key)
        stream = blob_client.download_blob()
        data = stream.readall()
    except Exception as e:
        logger.error(f"Blob not found or unreadable: {blob_key} | {e}")
        raise HTTPException(status_code=404, detail=f"Blob not found: {blob_key}")

    return StreamingResponse(
        io.BytesIO(data),
        media_type=content_type,
        headers={"Content-Disposition": "inline"},
    )


@router.get("/list/{prefix:path}", response_class=HTMLResponse)
async def list_blobs(prefix: str, request: Request):
    """
    List all blobs under a given prefix and render as clickable links.
    e.g. /blob/list/source_file_abc/crops
    """
    try:
        container_client = get_blob_client()._container_client
        blobs = container_client.list_blobs(name_starts_with=prefix)
        blob_names = [b.name for b in blobs]
    except Exception as e:
        logger.error(f"Failed to list blobs under prefix: {prefix} | {e}")
        raise HTTPException(status_code=500, detail="Failed to list blobs")

    if not blob_names:
        return HTMLResponse(content="<p>No blobs found under this prefix.</p>")
    base = str(request.base_url).rstrip("/")
    links = "\n".join(
        f'<li><a href="{base}/blob/view/{name}" target="_blank">{name}</a></li>'
        for name in blob_names
    )
    html = f"""
    <html>
    <head><title>Blob Viewer — {prefix}</title></head>
    <body>
        <h2>Blobs under: <code>{prefix}</code></h2>
        <ul>{links}</ul>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@router.delete("/delete/folder/{folder_name}")
async def delete_by_folder(folder_name: str):
    """Delete all blobs under a folder_name prefix."""
    try:
        container_client = get_blob_client()._container_client
        blobs = list(container_client.list_blobs(name_starts_with=f"{folder_name}/"))
        if not blobs:
            raise HTTPException(status_code=404, detail=f"No blobs found under folder: {folder_name}")
        for blob in blobs:
            container_client.delete_blob(blob.name)
        logger.info(f"Deleted {len(blobs)} blobs under folder={folder_name}")
        return {"deleted": len(blobs), "folder_name": folder_name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete folder {folder_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete/source/{folder_name}/{source_file}")
async def delete_by_source_file(folder_name: str, source_file: str):
    """Delete all blobs under folder_name/source_file prefix."""
    try:
        container_client = get_blob_client()._container_client
        prefix = f"{folder_name}/{source_file}/"
        blobs = list(container_client.list_blobs(name_starts_with=prefix))
        if not blobs:
            raise HTTPException(status_code=404, detail=f"No blobs found under: {prefix}")
        for blob in blobs:
            container_client.delete_blob(blob.name)
        logger.info(f"Deleted {len(blobs)} blobs under {prefix}")
        return {"deleted": len(blobs), "folder_name": folder_name, "source_file": source_file}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete source {folder_name}/{source_file}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
