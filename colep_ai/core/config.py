"""
Central config. Everything path-related lives here — no module in
ingestion/ should hardcode a path or read os.environ directly.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self):
        self.output_root = Path(os.environ.get("COLEP_OUTPUT_ROOT", "outputs")).resolve()
        self.gcp_key_path = Path(os.environ.get("GCP_VISION_KEY_PATH", "key.json")).resolve()
        self.anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

    # ---- per-document output tree ----
    def doc_root(self, source_file: str) -> Path:
        return self.output_root / source_file

    def pdf_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "pdf"

    def page_images_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "pdf_pages_images"

    def ocr_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "ocr"

    def crops_dir(self, source_file: str) -> Path:
        # extract_images_from_page writes {output_dir}/crops and {output_dir}/marked
        # so this IS the output_dir passed to it, per page subfolder.
        return self.doc_root(source_file) / "crops"

    def results_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "results"
    
    def combined_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "combined"

    def ensure_doc_dirs(self, source_file: str) -> None:
        for d in (
            self.pdf_dir(source_file),
            self.page_images_dir(source_file),
            self.ocr_dir(source_file),
            self.crops_dir(source_file),
            self.results_dir(source_file),
            self.combined_dir(source_file),
        ):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
