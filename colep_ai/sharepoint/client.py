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
"""

from __future__ import annotations

from pathlib import Path

import msal
import requests

from colep_ai.core.config import settings
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SCOPE = ["https://graph.microsoft.com/.default"]


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
        self._headers: dict[str, str] = {}
        self._drive_id: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict[str, str]:
        """
        Returns auth headers with a valid access token.
        MSAL caches the token and refreshes it before expiry automatically.
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

    def _get_drive_id(self) -> str:
        """Resolves and caches the SharePoint Documents drive ID."""
        if self._drive_id is not None:
            return self._drive_id

        headers = self._get_headers()

        # Step 1: resolve site ID
        site_url = f"{_GRAPH_BASE}/sites/{settings.SHAREPOINT_HOST}:{settings.SHAREPOINT_SITE_PATH}"
        resp = requests.get(site_url, headers=headers, timeout=30)
        resp.raise_for_status()
        site_id = resp.json()["id"]
        logger.info(f"[sharepoint] resolved site_id={site_id}")

        # Step 2: find the Documents drive
        drives_url = f"{_GRAPH_BASE}/sites/{site_id}/drives"
        resp = requests.get(drives_url, headers=headers, timeout=30)
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
        drive_id = self._get_drive_id()

        url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{folder_path}:/children"
        resp = requests.get(url, headers=headers, timeout=30)
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
    # Download
    # ------------------------------------------------------------------

    def download_file(self, item_id: str, save_path: Path) -> Path:
        """
        Downloads a single SharePoint file by item ID to save_path.
        Uses streaming to avoid loading large files into memory.

        Returns:
            save_path on success.

        Raises:
            requests.HTTPError on non-2xx response.
            IOError on write failure.
        """
        headers = self._get_headers()
        drive_id = self._get_drive_id()

        url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        resp = requests.get(url, headers=headers, stream=True, timeout=60)
        resp.raise_for_status()

        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.info(f"[sharepoint] downloaded {save_path.name} ({save_path.stat().st_size} bytes)")
        return save_path

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

        Returns:
            List of dicts: {name, local_path, size} for each successfully
            downloaded file. Failed individual downloads are logged and skipped
            — orchestrator decides how to handle partial downloads.

        Raises:
            RuntimeError if listing the folder itself fails (no files to process).
        """
        local_dir.mkdir(parents=True, exist_ok=True)

        xlsx_files = self.list_xlsx_files(folder_path)
        if not xlsx_files:
            raise RuntimeError(
                f"No .xlsx files found in SharePoint folder: '{folder_path}'"
            )

        downloaded: list[dict] = []
        for file_info in xlsx_files:
            save_path = local_dir / file_info["name"]
            try:
                self.download_file(file_info["id"], save_path)
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

    def download_file_by_path(self, file_path: str, local_dir: Path) -> dict:
        """
        Downloads a single file by its SharePoint path.
        e.g. /ShopFloor/Line5/O01.xlsx

        Returns:
            {name, local_path, size, sharepoint_path}

        Raises on any failure — orchestrator handles per-file error logging.
        """
        headers = self._get_headers()
        drive_id = self._get_drive_id()

        url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{file_path}:/content"
        resp = requests.get(url, headers=headers, stream=True, timeout=60)
        resp.raise_for_status()

        filename = Path(file_path).name
        save_path = local_dir / filename
        save_path.parent.mkdir(parents=True, exist_ok=True)

        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.info(f"[sharepoint] downloaded {filename} from '{file_path}'")
        return {
            "name": filename,
            "local_path": str(save_path),
            "size": save_path.stat().st_size,
            "sharepoint_path": file_path,
        }