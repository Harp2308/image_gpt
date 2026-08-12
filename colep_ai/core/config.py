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
    # ------------------------------------------------------------------
    # Anthropic
    # ------------------------------------------------------------------
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    ANTHROPIC_ENDPOINT: SecretStr = SecretStr("")
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"

    # ------------------------------------------------------------------
    # Google
    # ------------------------------------------------------------------
    GOOGLE_APPLICATION_CREDENTIALS: Path = Path("key.json")
    
    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------
    OPENAI_API_KEY: SecretStr = SecretStr("")
    OPENAI_ENDPOINT: SecretStr = SecretStr("")
    OPENAI_MODEL: str = "gpt-5.1"
    # ------------------------------------------------------------------
    # Embedings
    # ------------------------------------------------------------------

    EMBED_MODEL: str   = "text-embedding-3-large"        
    EMBED_DIM: int = 3072
    BATCH_SIZE: int = 100
        
    # ------------------------------------------------------------------
    # Qdrant (kept for legacy chat retrieval — not used in ingestion)
    # ------------------------------------------------------------------
    QDRANT_URL: str = ""
    QDRANT_API_KEY: SecretStr = SecretStr("")

    # ------------------------------------------------------------------
    # Azure AI Search
    # ------------------------------------------------------------------
    AZURE_SEARCH_ENDPOINT: str = ""
    AZURE_SEARCH_API_KEY: SecretStr = SecretStr("")
    INDEX_NAME :str =""

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    # ------------------------------------------------------------------
    # Celery
    # ------------------------------------------------------------------
    CELERY_BROKER_URL: str = "redis://127.0.0.1:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://127.0.0.1:6379/2"

    # ------------------------------------------------------------------
    # SharePoint / Microsoft Graph
    # ------------------------------------------------------------------
    AZURE_TENANT_ID: str = ""
    AZURE_CLIENT_ID: str = ""
    AZURE_CLIENT_SECRET: SecretStr = SecretStr("")
    SHAREPOINT_HOST: str = ""        # e.g. contoso.sharepoint.com
    SHAREPOINT_SITE_PATH: str = ""   # e.g. /sites/colep

    # ------------------------------------------------------------------
    # Azure Blob Storage
    # ------------------------------------------------------------------

    AZURE_STORAGE_CONNECTION_STRING: SecretStr = SecretStr("")
    AZURE_STORAGE_CONTAINER_NAME: str = "colep-ai"
    # ------------------------------------------------------------------
    # Cohere
    # ------------------------------------------------------------------
    COHERE_API_KEY: SecretStr = SecretStr("")
    
    # ------------------------------------------------------------------
    # Ingestion pipeline tuning
    # ------------------------------------------------------------------

    # Max parallel pages processed inside a single Excel ingest task.
    # Keep low (3-5) to avoid hammering Claude API rate limits.
    # With N Celery ingestion workers each running PAGE_THREAD_WORKERS
    # threads, total concurrent Claude calls = N * PAGE_THREAD_WORKERS.
    PAGE_THREAD_WORKERS: int = 3

    # Fraction of pages that may fail before the entire file is marked
    # failed and indexing is skipped. 0.5 = up to 50% page failures
    # are tolerated; the file proceeds to indexing with partial results.
    # Set to 0.0 to require all pages to succeed.
    INGEST_PAGE_FAILURE_THRESHOLD: float = 0.5

    # Redis TTL for job and file tracker keys (seconds). 7 days default.
    JOB_TRACKER_TTL_S: int = 7 * 24 * 60 * 60

    # Root directory for temporarily downloaded SharePoint Excel files.
    # Each job gets its own subdirectory: downloads/{job_id}/
    # Deleted by cleanup_task after successful indexing.
    DOWNLOADS_ROOT: Path = Path("downloads")

    # ------------------------------------------------------------------
    # Cosmos DB  (conversation history)
    # ------------------------------------------------------------------
    COSMOS_URL: str = "https://localhost:8081"          # local emulator default
    COSMOS_KEY: SecretStr =  SecretStr("")  # emulator master key
    COSMOS_DB_NAME: str = "colep_ai"
    COSMOS_CONTAINER_NAME: str = "chat_sessions"
 
    # ------------------------------------------------------------------
    # Session / history tuning
    # ------------------------------------------------------------------
    SESSION_TTL_SECONDS: int = 86400        # 24h sliding TTL on Redis keys
    SESSION_HISTORY_WINDOW: int = 3         # number of full turns kept verbatim
    SUMMARY_MODEL: str = "gpt-5.1"  # model used for incremental summarisation
 

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    # ==============================================================
    # Output directory helpers
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

    def downloads_dir(self, job_id: str) -> Path:
        """Temporary download location for a specific ingestion job."""
        return self.DOWNLOADS_ROOT / job_id

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