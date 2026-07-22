# watchdog.py — run from project root
#
# Responsibilities:
#   1. Detect hung worker via stale Redis heartbeat → OS-level kill + respawn
#   2. Periodic forced restart every FORCE_RESTART_INTERVAL_S regardless of health
#      — clears accumulated orphaned threads that heartbeat cannot detect
#
# Design decisions:
#   - Uses psutil.Process.kill() (TerminateProcess on Windows) — immune to GIL hang
#   - subprocess.Popen with CREATE_NEW_CONSOLE — worker gets its own terminal,
#     watchdog death does not cascade to worker and vice versa
#   - Heartbeat TTL (20s) and poll interval (10s) are intentionally conservative —
#     avoids false-positive kills on transient Redis blips
#   - No dependency on Celery inspect/ping — those go through the broker and will
#     silently hang if the worker's broker connection is itself the problem
#
# Queue layout:
#   orchestration  — lightweight fanout, concurrency=2
#   ingestion      — heavy win32com + Claude, concurrency=4 (Windows only)
#   indexing       — embed + Azure Search upsert, concurrency=8
#   cleanup        — delete xlsx + update counters, concurrency=8

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import psutil
import redis

# ── Configuration ─────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
HEARTBEAT_KEY_PREFIX = "worker:hb:"
STALE_THRESHOLD_S = 25             # heartbeat older than this = worker is hung
POLL_INTERVAL_S = 10               # how often watchdog checks heartbeat keys
FORCE_RESTART_INTERVAL_S = 6 * 60 * 60  # 6 hours — proactive leak prevention

PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

CELERY_APP = "colep_ai.worker.celery_app"

WORKERS = {
    "worker.orchestration": [
        str(PYTHON), "-m", "celery",
        "-A", CELERY_APP, "worker",
        "--pool=threads",
        "--concurrency=2",
        "-Q", "orchestration",
        "--loglevel=info",
        "-n", "worker.orchestration@%h",
    ],
    "worker.ingestion": [
        str(PYTHON), "-m", "celery",
        "-A", CELERY_APP, "worker",
        "--pool=threads",
        "--concurrency=4",
        "-Q", "ingestion",
        "--loglevel=info",
        "-n", "worker.ingestion@%h",
    ],
    "worker.indexing": [
        str(PYTHON), "-m", "celery",
        "-A", CELERY_APP, "worker",
        "--pool=threads",
        "--concurrency=8",
        "-Q", "indexing",
        "--loglevel=info",
        "-n", "worker.indexing@%h",
    ],
    "worker.cleanup": [
        str(PYTHON), "-m", "celery",
        "-A", CELERY_APP, "worker",
        "--pool=threads",
        "--concurrency=8",
        "-Q", "cleanup",
        "--loglevel=info",
        "-n", "worker.cleanup@%h",
    ],
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | watchdog | %(message)s",
)
log = logging.getLogger("watchdog")

# ── Redis ─────────────────────────────────────────────────────────────────────

r = redis.from_url(REDIS_URL, socket_connect_timeout=5, socket_timeout=5)

# ── State ─────────────────────────────────────────────────────────────────────

_processes: dict[str, subprocess.Popen] = {}
_last_forced_restart: dict[str, float] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _spawn(name: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        WORKERS[name],
        cwd=str(PROJECT_ROOT),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    log.info(f"spawned {name} pid={proc.pid}")
    return proc


def _kill(name: str, pid: int) -> None:
    try:
        psutil.Process(pid).kill()
        log.info(f"killed {name} pid={pid}")
    except psutil.NoSuchProcess:
        log.warning(f"kill {name} pid={pid} — process already gone")


def _hostname() -> str:
    import socket
    return socket.gethostname()


def _heartbeat(name: str) -> tuple[bool, int | None]:
    """Returns (is_stale, pid). is_stale=True also when key is missing."""
    try:
        raw = r.get(f"{HEARTBEAT_KEY_PREFIX}{name}@{_hostname()}")
        if raw is None:
            return True, None
        data = json.loads(raw)
        age = time.time() - data["ts"]
        return age > STALE_THRESHOLD_S, data.get("pid")
    except Exception as exc:
        log.error(f"redis error checking {name}: {exc}")
        return False, None  # don't kill on Redis failure — unknown state


def _due_for_forced_restart(name: str) -> bool:
    last = _last_forced_restart.get(name, 0)
    return (time.time() - last) >= FORCE_RESTART_INTERVAL_S


def _handle_worker(name: str) -> None:
    proc = _processes.get(name)

    # Worker not spawned yet or exited on its own — spawn fresh
    if proc is None or proc.poll() is not None:
        log.info(f"{name} not running — spawning")
        _processes[name] = _spawn(name)
        _last_forced_restart[name] = time.time()
        return

    # Forced periodic restart — proactive, health-independent
    if _due_for_forced_restart(name):
        log.info(f"{name} scheduled restart (6h interval)")
        _kill(name, proc.pid)
        _processes[name] = _spawn(name)
        _last_forced_restart[name] = time.time()
        return

    # Heartbeat stale — worker is hung
    stale, pid = _heartbeat(name)
    if stale:
        log.warning(f"{name} heartbeat stale — killing and respawning")
        _kill(name, pid or proc.pid)
        _processes[name] = _spawn(name)
        _last_forced_restart[name] = time.time()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info(f"watchdog started — monitoring: {list(WORKERS)}")
    log.info(
        f"stale_threshold={STALE_THRESHOLD_S}s "
        f"poll={POLL_INTERVAL_S}s "
        f"force_restart={FORCE_RESTART_INTERVAL_S}s"
    )
    while True:
        for name in WORKERS:
            try:
                _handle_worker(name)
            except Exception as exc:
                log.error(f"unhandled error in watchdog loop for {name}: {exc}")
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
