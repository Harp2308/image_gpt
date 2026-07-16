from datetime import datetime
from pathlib import Path
import threading
from openpyxl import load_workbook, Workbook

_lock = threading.Lock()
_HEADERS = ["timestamp", "question", "language", "model", "answer"]

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


def log_query(question: str, language: str, model: str, answer: str) -> None:
    with _lock:
        path = _get_log_path()
        _ensure_file(path)
        wb = load_workbook(path)
        ws = wb["Queries"]
        ws.append([datetime.utcnow().isoformat(), question, language, model, answer])
        wb.save(path)