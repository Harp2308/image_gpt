"""
blob_storage.py
---------------
Azure Blob Storage utility for Colep AI ingestion pipeline.

Responsibility:
  - Upload local files to blob (after local write is already done)
  - Blob key (path) generation — mirrors local output directory structure
  - One BlobStorageClient instance shared across the pipeline (singleton via module-level)

What this is NOT:
  - Not a replacement for local disk writes
  - Not handling cache/existence checks (local Path.exists() handles that)
  - Not doing any image processing
"""

from pathlib import Path
from azure.storage.blob import BlobServiceClient, ContentSettings
from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

# -----------------------------------------------------------------
# MIME type map — so blob is served with correct Content-Type
# when viewed in Azure portal or accessed via URL
# -----------------------------------------------------------------
_CONTENT_TYPE_MAP = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json",
}


# =================================================================
# Blob Key Helpers — mirrors config.py local path helpers exactly
# =================================================================

def blob_pdf_key(source_file: str, filename: str) -> str:
    """e.g. source_file_abc/pdf/source_file_abc.pdf"""
    return f"{source_file}/pdf/{filename}"


def blob_page_image_key(source_file: str, filename: str) -> str:
    """e.g. source_file_abc/pdf_pages_images/source_file_abc_page_1.png"""
    return f"{source_file}/pdf_pages_images/{filename}"


def blob_crop_key(source_file: str, page_number: int, filename: str) -> str:
    """e.g. source_file_abc/crops/page_1/crops/page_1_a3f2.png"""
    return f"{source_file}/crops/page_{page_number}/crops/{filename}"


def blob_marked_key(source_file: str, page_number: int, filename: str) -> str:
    """e.g. source_file_abc/crops/page_1/marked/page_1_marked.png"""
    return f"{source_file}/crops/page_{page_number}/marked/{filename}"


def blob_combined_key(source_file: str, page_number: int, filename: str) -> str:
    """e.g. source_file_abc/combined/page_1/page_1_step1_uid.png"""
    return f"{source_file}/combined/page_{page_number}/{filename}"


# =================================================================
# Client
# =================================================================

class BlobStorageClient:
    """
    Thin wrapper around azure-storage-blob.
    One method: upload_file().
    Everything else (local writes, cache checks) stays in the pipeline.
    """

    def __init__(self) -> None:
        conn_str = settings.AZURE_STORAGE_CONNECTION_STRING.get_secret_value()
        if not conn_str:
            raise ValueError(
                "AZURE_STORAGE_CONNECTION_STRING is not set in .env — "
                "blob upload will not work."
            )
        self._container = settings.AZURE_STORAGE_CONTAINER_NAME
        self._client = BlobServiceClient.from_connection_string(conn_str)
        self._container_client = self._client.get_container_client(self._container)
        try:
            self._container_client.create_container()
            logger.info(f"Container created | {self._container}")
        except Exception:
            logger.info(f"Container already exists | {self._container}")
        logger.info(f"BlobStorageClient initialized | container={self._container}")

    def upload_file(self, local_path: str | Path, blob_key: str) -> bool:
        """
        Upload a local file to blob storage.

        - Overwrites if blob already exists (overwrite=True)
        - Sets Content-Type based on file extension
        - Returns True on success, False on failure (logs error, does NOT raise)
          so a blob upload failure never crashes the pipeline

        Args:
            local_path: absolute or relative path to the file on local disk
            blob_key:   destination path inside the container
                        (use blob_*_key() helpers above to generate this)
        """
        local_path = Path(local_path)

        if not local_path.exists():
            logger.error(f"upload_file: local file does not exist: {local_path}")
            return False

        suffix = local_path.suffix.lower()
        content_type = _CONTENT_TYPE_MAP.get(suffix, "application/octet-stream")
        content_settings = ContentSettings(content_type=content_type)

        try:
            with open(local_path, "rb") as f:
                self._container_client.upload_blob(
                    name=blob_key,
                    data=f,
                    overwrite=True,
                    content_settings=content_settings,
                )
            logger.info(f"Uploaded → blob://{self._container}/{blob_key}")
            return True

        except Exception:
            logger.exception(
                f"upload_file failed | local={local_path} | blob_key={blob_key}"
            )
            return False


# =================================================================
# Module-level singleton
# Lazy init — only created when first accessed, not at import time.
# This avoids crashing the entire app if blob creds are missing
# (e.g. during local dev without .env configured).
# =================================================================

_blob_client: BlobStorageClient | None = None


def get_blob_client() -> BlobStorageClient:
    global _blob_client
    if _blob_client is None:
        _blob_client = BlobStorageClient()
    return _blob_client
