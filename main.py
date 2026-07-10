import argparse
import json
import logging
import time
from datetime import datetime

import anthropic

from colep_ai.core.config import settings
from colep_ai.ingestion_V2.ocr_extractor import get_vision_client
from colep_ai.ingestion_V2.pipeline import run_pipeline,get_total_pages

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def main(excel_path:str):
    vision_client = get_vision_client(str(settings.GOOGLE_APPLICATION_CREDENTIALS))
    claude_client = anthropic.Anthropic()
    total_pages = get_total_pages(excel_path)

    for page in range(1, total_pages + 1):
        try:
            run_pipeline(
                excel_path=excel_path,
                page_number=page,
                vision_client=vision_client,
                claude_client=claude_client,
            )
            logger.info(f"Page{page}done")
        except Exception as e:
            logger.error(f"Page{ page} failed: {e}", exc_info=True)
            continue

if __name__ == "__main__":
    main(r"docs\e1.xlsx")