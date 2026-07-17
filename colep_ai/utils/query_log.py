from datetime import datetime
from pathlib import Path
import threading
from openpyxl import load_workbook, Workbook

_lock = threading.Lock()
_HEADERS = ["timestamp", "question", "language", "model", "answer", "context"]

# xlsx hard limit is 32,767 chars per cell (openpyxl raises past this).
# Context blocks can easily exceed that with a handful of retrieved
# entries, so we truncate defensively rather than let logging crash
# the request path.
_MAX_CELL_CHARS = 32000

LOG_BASE = Path(__file__).resolve().parent.parent.parent / "logs"


def _get_log_path() -> Path:
    day_dir = LOG_BASE / datetime.utcnow().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / "query_log.xlsx"


def _ensure_file(path: Path):
    if not path.exists():
        wb = Workbook()
        ws = wb.active
        ws.title = "Queries"
        ws.append(_HEADERS)
        wb.save(path)


def log_query(question: str, language: str, model: str, answer: str, context: str = "") -> None:
    if len(context) > _MAX_CELL_CHARS:
        context = context[:_MAX_CELL_CHARS] + f"...[TRUNCATED, {len(context)} total chars]"

    with _lock:
        path = _get_log_path()
        _ensure_file(path)
        wb = load_workbook(path)
        ws = wb["Queries"]
        ws.append([datetime.utcnow().isoformat(), question, language, model, answer, context])
        wb.save(path)