
import logging
import time
from datetime import datetime

import anthropic

from colep_ai.core.config import settings
from colep_ai.indexing.pipeline import run_indexing
from colep_ai.indexing.embedder import get_openai_client
from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

openai_client = get_openai_client()

def main(dir_path:str):
    start_time=datetime.now()
    logger.info(f"process started at={start_time.strftime('%Y-%m-%dT%H:%M:%S')} ")

    run_indexing(dir_path,openai_client)

    end_time = datetime.now()
    duration = end_time - start_time
    logger.info(f"process ended at={end_time.strftime('%Y-%m-%dT%H:%M:%S')} duration={str(duration)}")


if __name__ == "__main__":
    d=r"D:\Harpreet Data\1_PROJECTS\Colep_ai\colepV1\outputs\e1\results"
    main(d)