import argparse
import json
import logging
import time
from datetime import datetime

import anthropic

from colep_ai.core.config import settings
from colep_ai.ingestion.ocr_extractor import get_vision_client
from colep_ai.ingestion.pipeline import run_pipeline,get_total_pages
from colep_ai.generation.claude_client import get_claude_client_sync
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def main(excel_path:str):
    claude_client = get_claude_client_sync()
    total_pages = get_total_pages(excel_path)

    for page in range(1, total_pages + 1):
        try:
            run_pipeline(
                excel_path=excel_path,
                page_number=page,
                claude_client=claude_client,
                folder_name="flowchart"
            )
            logger.info(f"Page{page}done")
        except Exception as e:
            logger.error(f"Page{ page} failed: {e}", exc_info=True)
            continue

if __name__ == "__main__":    

    from pathlib import Path

    DOCS_DIR = Path(r"documents")

    excel_files = [
        # DOCS_DIR / "O01.M016.1 - Manutenção Autónoma L5 Montagem 1.xlsx",
        DOCS_DIR / "O01.M019.1- Instrução de Manutenção Autónoma L63 1.xlsx",
    ]

    for excel_file in excel_files:
        try:
            logger.info(f"Processing: {excel_file.name}")
            main(str(excel_file))
        except Exception as e:
            logger.warning(f"❌ Failed: {excel_file.name}")
            logger.error(e)
    # main(r"docs\e1.xlsx")
    # from pathlib import Path
    # import openpyxl

    # DOCS_DIR = Path(r"documents")
    # excel_files = list(DOCS_DIR.glob("*.xlsx"))

    # for excel_file in excel_files:
    #     try:
    #         logger.info(f"Processing: {excel_file.name}")
    #         # wb = openpyxl.load_workbook(excel_file, data_only=True)
    #         # for ws in wb.worksheets:
    #         #     print(ws.title, ws.print_area, ws.dimensions)
    #         main(str(excel_file))
    #     except Exception as e:
    #         logger.warning(f"❌ Failed: {excel_file.name}")
    #         logger.error(e)
    # main(r"docs\O01.T025.3 - Parâmetros do forno e PU - Linha 63 Estampagem 1.xlsx")