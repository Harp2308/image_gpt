"""
sharepoint/client.py

Microsoft Graph API client for downloading .xlsx files from a SharePoint folder.

Responsibilities:
  - Acquire an app-only OAuth2 token via MSAL (client credentials flow)
  - Resolve SharePoint site ID and Documents drive ID
  - List .xlsx files in the specified folder path (non-recursive)
  - Download each file to a local directory

Design decisions:
  - Non-recursive: only top-level .xlsx files in the given folder are downloaded.
    Subfolders are ignored. This matches the finalised scope.
  - Token is acquired fresh per SharePointClient instance. For long-running
    jobs, MSAL's ConfidentialClientApplication caches the token and refreshes
    it automatically before expiry.
  - All HTTP errors raise immediately — orchestrator_task handles retry logic.
  - Streaming download (iter_content) prevents large files loading into memory.

Timeout strategy:
  - All metadata/listing calls use timeout=(10, 30): 10s connect, 30s read.
    These are small JSON responses — 30s read is already generous.
  - Download calls use timeout=(10, 300): 10s connect, 300s read.
    300s read timeout is per-chunk, not total transfer time. At 512KB chunks
    a 7MB file makes ~14 read calls — each gets a full 300s window.
    This is the correct model for streaming large files over a corporate tenant.

Retry strategy (tenacity):
  - 3 attempts total, exponential backoff: 4s → 8s → 16s (capped at 30s).
  - Retries on requests.Timeout and requests.ConnectionError only.
  - Does NOT retry on requests.HTTPError (4xx/5xx are not transient — they
    indicate auth failure, missing file, or permissions issue).
  - The entire request+stream is retried from scratch. Graph API does not
    support reliable range requests on SharePoint drive items, so partial
    resume is not safe. The temp file is truncated before each retry attempt.

Chunk size:
  - 512KB (512 * 1024 bytes). For a 4MB file this is ~8 read calls vs ~465
    at the default 8192. Fewer round trips = less exposure to per-read timeout.
"""

from __future__ import annotations

from pathlib import Path

import msal
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    RetryError,
)
import logging
from urllib.parse import quote
from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SCOPE = ["https://graph.microsoft.com/.default"]

# Timeout for metadata/listing calls: (connect_timeout, read_timeout)
_META_TIMEOUT = (10, 30)

# Timeout for file download streaming: (connect_timeout, read_timeout)
# read_timeout applies per chunk read, not total transfer duration.
_DOWNLOAD_TIMEOUT = (10, 300)

# Chunk size for streaming downloads.
# 512KB reduces read call count ~60x vs the previous 8192 default.
_CHUNK_SIZE = 512 * 1024

# Tenacity retry decorator for download operations.
# Retries on transient network failures only.
_download_retry = retry(
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


class SharePointClient:
    """
    Thin wrapper around Microsoft Graph API for SharePoint file operations.
    One instance per ingestion job is sufficient — MSAL handles token caching.
    """

    def __init__(self) -> None:
        self._msal_app = msal.ConfidentialClientApplication(
            settings.AZURE_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}",
            client_credential=settings.AZURE_CLIENT_SECRET.get_secret_value(),
        )
        self._drive_id: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict[str, str]:
        """
        Returns auth headers with a valid access token.
        MSAL caches the token and refreshes it before expiry automatically.
        Call this once per logical operation (e.g. once per download_folder
        call), not once per file in a loop.
        """
        result = self._msal_app.acquire_token_for_client(scopes=_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                f"Failed to acquire SharePoint token: {result.get('error_description', result)}"
            )
        return {"Authorization": f"Bearer {result['access_token']}"}

    # ------------------------------------------------------------------
    # Drive resolution (cached per instance)
    # ------------------------------------------------------------------

    def _get_drive_id(self, headers: dict[str, str]) -> str:
        """
        Resolves and caches the SharePoint Documents drive ID.

        Args:
            headers: Auth headers from _get_headers(). Caller is responsible
                     for resolving headers once and passing them in — avoids
                     redundant token acquisition in loops.
        """
        if self._drive_id is not None:
            return self._drive_id

        # Step 1: resolve site ID
        site_url = f"{_GRAPH_BASE}/sites/{settings.SHAREPOINT_HOST}:{settings.SHAREPOINT_SITE_PATH}"
        resp = requests.get(site_url, headers=headers, timeout=_META_TIMEOUT)
        resp.raise_for_status()
        site_id = resp.json()["id"]
        logger.info(f"[sharepoint] resolved site_id={site_id}")

        # Step 2: find the Documents drive
        drives_url = f"{_GRAPH_BASE}/sites/{site_id}/drives"
        resp = requests.get(drives_url, headers=headers, timeout=_META_TIMEOUT)
        resp.raise_for_status()
        drives = resp.json()["value"]

        doc_drive = next(
            (d for d in drives if d["name"] == "Documents"),
            None,
        )
        if doc_drive is None:
            raise RuntimeError(
                f"'Documents' drive not found in SharePoint site. "
                f"Available drives: {[d['name'] for d in drives]}"
            )

        self._drive_id = doc_drive["id"]
        logger.info(f"[sharepoint] resolved drive_id={self._drive_id}")
        return self._drive_id

    # ------------------------------------------------------------------
    # List files
    # ------------------------------------------------------------------

    def list_xlsx_files(self, folder_path: str) -> list[dict]:
        """
        Lists all .xlsx files directly inside folder_path (non-recursive).

        Args:
            folder_path: SharePoint-relative path, e.g. '/ShopFloor/Line5'

        Returns:
            List of dicts with keys: id, name, size
        """
        headers = self._get_headers()
        drive_id = self._get_drive_id(headers)

        encoded_path = quote(folder_path.strip("/"))
        url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}:/children"
        logger.info(f"[sharepoint] list url: {url}")
        resp = requests.get(url, headers=headers, timeout=_META_TIMEOUT)
        resp.raise_for_status()

        items = resp.json().get("value", [])
        xlsx_files = [
            {"id": item["id"], "name": item["name"], "size": item.get("size", 0)}
            for item in items
            if "file" in item and item["name"].endswith(".xlsx")
        ]

        logger.info(
            f"[sharepoint] found {len(xlsx_files)} .xlsx files in '{folder_path}'"
        )
        return xlsx_files

    # ------------------------------------------------------------------
    # Download (single file, by item ID)
    # ------------------------------------------------------------------

    def download_file(
        self,
        item_id: str,
        save_path: Path,
        headers: dict[str, str] | None = None,
    ) -> Path:
        """
        Downloads a single SharePoint file by item ID to save_path.

        Retries up to 3 times on Timeout or ConnectionError with exponential
        backoff (4s, 8s, 16s). Each retry re-requests the file from scratch —
        Graph API does not guarantee range request support on SharePoint drives.

        Args:
            item_id: SharePoint drive item ID.
            save_path: Local path to write the file to.
            headers: Auth headers. If None, resolved internally. Pass headers
                     explicitly when calling in a loop to avoid redundant
                     token acquisition per file.

        Returns:
            save_path on success.

        Raises:
            requests.HTTPError on non-2xx response (not retried).
            requests.Timeout / requests.ConnectionError after 3 failed attempts.
            IOError on write failure.
        """
        if headers is None:
            headers = self._get_headers()

        drive_id = self._get_drive_id(headers)
        url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        save_path.parent.mkdir(parents=True, exist_ok=True)

        @_download_retry
        def _attempt() -> None:
            # Truncate before each attempt — partial writes from a previous
            # attempt must not corrupt the final file.
            with open(save_path, "wb") as f:
                resp = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=_DOWNLOAD_TIMEOUT,
                )
                resp.raise_for_status()  # HTTPError is not retried
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    f.write(chunk)

        _attempt()

        size = save_path.stat().st_size
        logger.info(f"[sharepoint] downloaded {save_path.name} ({size} bytes)")
        return save_path

    # ------------------------------------------------------------------
    # Download (single file, by SharePoint path)
    # ------------------------------------------------------------------

    def download_file_by_path(
        self,
        file_path: str,
        local_dir: Path,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """
        Downloads a single file by its SharePoint path.
        e.g. /ShopFloor/Line5/O01.xlsx

        Same retry and timeout behaviour as download_file.

        Args:
            file_path: SharePoint-relative file path.
            local_dir: Local directory to write the file into.
            headers: Auth headers. If None, resolved internally.

        Returns:
            {name, local_path, size, sharepoint_path}

        Raises on any failure — orchestrator handles per-file error logging.
        """
        if headers is None:
            headers = self._get_headers()

        drive_id = self._get_drive_id(headers)
        encoded_path = quote(file_path.strip("/"))
        url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}:/content"

        filename = Path(file_path).name
        save_path = local_dir / filename
        save_path.parent.mkdir(parents=True, exist_ok=True)

        @_download_retry
        def _attempt() -> None:
            with open(save_path, "wb") as f:
                resp = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=_DOWNLOAD_TIMEOUT,
                )
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    f.write(chunk)

        _attempt()

        logger.info(f"[sharepoint] downloaded {filename} from '{file_path}'")
        return {
            "name": filename,
            "local_path": str(save_path),
            "size": save_path.stat().st_size,
            "sharepoint_path": file_path,
        }

    # ------------------------------------------------------------------
    # Bulk download (used by orchestrator_task)
    # ------------------------------------------------------------------

    def download_folder(
        self,
        folder_path: str,
        local_dir: Path,
    ) -> list[dict]:
        """
        Downloads all .xlsx files from folder_path to local_dir.

        Resolves auth headers once for the entire batch — avoids one token
        acquire call per file.

        Returns:
            List of dicts: {name, local_path, size, sharepoint_path} for each
            successfully downloaded file. Failed individual downloads are logged
            and skipped — orchestrator decides how to handle partial downloads.

        Raises:
            RuntimeError if listing the folder itself fails (no files to process).
        """
        local_dir.mkdir(parents=True, exist_ok=True)

        # Resolve headers once for the whole batch.
        headers = self._get_headers()

        xlsx_files = self.list_xlsx_files(folder_path)
        if not xlsx_files:
            raise RuntimeError(
                f"No .xlsx files found in SharePoint folder: '{folder_path}'"
            )

        downloaded: list[dict] = []
        for file_info in xlsx_files:
            save_path = local_dir / file_info["name"]
            try:
                self.download_file(file_info["id"], save_path, headers=headers)
                downloaded.append({
                    "name": file_info["name"],
                    "local_path": str(save_path),
                    "size": file_info["size"],
                    "sharepoint_path": f"{folder_path}/{file_info['name']}",
                })
            except Exception:
                logger.exception(
                    f"[sharepoint] failed to download '{file_info['name']}' — skipping"
                )

        logger.info(
            f"[sharepoint] download complete: "
            f"{len(downloaded)}/{len(xlsx_files)} files downloaded to {local_dir}"
        )
        return downloaded