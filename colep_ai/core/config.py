from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="COLEP_", extra="ignore")

    base_dir: Path = Field(default=Path(__file__).resolve().parent.parent)
    gcp_key_filename: str = r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\key.json"
    pdf_render_dpi: int = 200

    output_dir_name: str = "outputs"
    pdfs_dir_name: str = "pdfs"
    page_images_dir_name: str = "page_images"
    crops_dir_name: str = "crops"
    results_dir_name: str = "results"
    reconstructed_dir_name: str = "reconstructed"

    @property
    def output_dir(self) -> Path: return self.base_dir / self.output_dir_name
    @property
    def pdfs_dir(self) -> Path: return self.output_dir / self.pdfs_dir_name
    @property
    def page_images_dir(self) -> Path: return self.output_dir / self.page_images_dir_name
    @property
    def crops_dir(self) -> Path: return self.output_dir / self.crops_dir_name
    @property
    def results_dir(self) -> Path: return self.output_dir / self.results_dir_name
    @property
    def reconstructed_dir(self) -> Path: return self.output_dir / self.reconstructed_dir_name
    @property
    def gcp_key_path(self) -> Path: return self.base_dir / self.gcp_key_filename

    def ensure_dirs(self) -> None:
        for d in [self.pdfs_dir, self.page_images_dir, self.crops_dir,
                  self.results_dir, self.reconstructed_dir]:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()