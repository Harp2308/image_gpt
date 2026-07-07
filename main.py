import argparse
import json
import logging
import time
from datetime import datetime

import anthropic

from colep_ai.core.config import settings
from colep_ai.ingestion_V2.ocr_extractor import get_vision_client
from colep_ai.ingestion_V2.pipeline import run_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def main(excel_path:str,page:int):
    # parser = argparse.ArgumentParser()
    # parser.add_argument("--excel", required=True, help="Path to .xlsx file")
    # parser.add_argument("--page", type=int, required=True, help="1-based page number")
    # args = parser.parse_args()

    vision_client = get_vision_client(str(settings.gcp_key_path))
    claude_client = anthropic.Anthropic()

    result = run_pipeline(
        excel_path=excel_path,
        page_number=page,
        vision_client=vision_client,
        claude_client=claude_client,
    )

    # print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    excel_path=r"docs\e5.xlsx"
    p=3
    main(excel_path,p)
