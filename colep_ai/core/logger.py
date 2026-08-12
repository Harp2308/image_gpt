import sys
from pathlib import Path
from loguru import logger as _loguru_logger
import logging

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Remove default loguru handler
_loguru_logger.remove()

# Console
_loguru_logger.add(
    sys.stdout,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}\n{exception}",
    level="INFO",
    colorize=True,
    enqueue=True,
)

# File — new folder per day, single writer via queue
_loguru_logger.add(
    str(LOG_DIR / "{time:YYYY-MM-DD}" / "analysis.log"),
    rotation="00:00",
    retention="10 days",
    encoding="utf-8",
    enqueue=True,  # thread/process safe — single writer queue
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}\n{exception}",
    level="INFO",
)


class _InterceptHandler(logging.Handler):
    """
    Redirect any stdlib logging (celery, httpx, etc.) into loguru.
    """
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = _loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        _loguru_logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


# Patch stdlib root logger so third-party libs also go through loguru
logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)
logging.getLogger("httpx").setLevel(logging.WARNING)

logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.core").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies").setLevel(logging.WARNING)
def get_logger(name: str):
    return _loguru_logger.bind(name=name)