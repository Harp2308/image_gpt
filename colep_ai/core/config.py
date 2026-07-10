from pathlib import Path
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------
    ENV: str = "development"

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    OUTPUT_ROOT: Path = Path("outputs")
    GCP_VISION_KEY_PATH: Path = Path("key.json")

    # ------------------------------------------------------------------
    # Anthropic
    # ------------------------------------------------------------------
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"

    # ------------------------------------------------------------------
    # Google
    # ------------------------------------------------------------------
    GOOGLE_APPLICATION_CREDENTIALS: Path = Path("key.json")

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------
    OPENAI_API_KEY: SecretStr = SecretStr("")

    # ------------------------------------------------------------------
    # Qdrant
    # ------------------------------------------------------------------
    QDRANT_URL: str = ""
    QDRANT_API_KEY: SecretStr = SecretStr("")


    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    # ==============================================================
    # Output Directory Helpers
    # ==============================================================

    def doc_root(self, source_file: str) -> Path:
        return self.OUTPUT_ROOT / source_file

    def pdf_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "pdf"

    def page_images_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "pdf_pages_images"

    def ocr_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "ocr"

    def crops_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "crops"

    def results_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "results"

    def combined_dir(self, source_file: str) -> Path:
        return self.doc_root(source_file) / "combined"

    def ensure_doc_dirs(self, source_file: str) -> None:
        directories = (
            self.pdf_dir(source_file),
            self.page_images_dir(source_file),
            self.ocr_dir(source_file),
            self.crops_dir(source_file),
            self.results_dir(source_file),
            self.combined_dir(source_file),
        )

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()