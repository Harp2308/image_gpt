"""
worker/celery_app.py

Celery application instance for the Colep AI ingestion pipeline.

Queue layout:
  orchestration  — lightweight: download SharePoint files + fan out chains
  ingestion      — heavy: Excel→PDF→PNGs→Claude per file (Windows only, win32com)
  indexing       — medium: embed + Azure AI Search upsert per file
  cleanup        — trivial: delete downloaded .xlsx + update job completion

IMPORTANT — Windows constraint:
  The ingestion queue workers MUST run on Windows because excel_to_pdf()
  uses win32com (Excel COM automation) to convert .xlsx → .pdf.
  win32com is not available on Linux/macOS.
  Indexing and cleanup workers have no such constraint.

Worker startup commands (run from project root):
  celery -A colep_ai.worker.celery_app worker -Q orchestration -n orchestration@%h --pool=threads --concurrency=2 --loglevel=info
  celery -A colep_ai.worker.celery_app worker -Q ingestion     -n ingestion@%h     --pool=threads --concurrency=4 --loglevel=info
  celery -A colep_ai.worker.celery_app worker -Q indexing      -n indexing@%h      --pool=threads --concurrency=8 --loglevel=info
  celery -A colep_ai.worker.celery_app worker -Q cleanup       -n cleanup@%h       --pool=threads --concurrency=8 --loglevel=info
"""

from celery import Celery
from kombu import Exchange, Queue

from colep_ai.core.config import settings
import colep_ai.worker.worker_heartbeat  

# ---------------------------------------------------------------------------
# Queue and exchange definitions
# ---------------------------------------------------------------------------

_default_exchange = Exchange("colep", type="direct")

QUEUES = (
    Queue("orchestration", _default_exchange, routing_key="orchestration"),
    Queue("ingestion",     _default_exchange, routing_key="ingestion"),
    Queue("indexing",      _default_exchange, routing_key="indexing"),
    Queue("cleanup",       _default_exchange, routing_key="cleanup"),
)

# ---------------------------------------------------------------------------
# Celery app
# ---------------------------------------------------------------------------

celery_app = Celery(
    "colep_ai",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "colep_ai.worker.tasks.orchestrator",
        "colep_ai.worker.tasks.ingest",
        "colep_ai.worker.tasks.index",
        "colep_ai.worker.tasks.cleanup",
    ],
)

celery_app.conf.update(
    # ── Queue routing ──────────────────────────────────────────────────────
    task_queues=QUEUES,
    task_default_queue="ingestion",
    task_default_exchange="colep",
    task_default_routing_key="ingestion",
    task_routes={
        "colep_ai.worker.tasks.orchestrator.*": {
            "queue": "orchestration",
            "routing_key": "orchestration",
        },
        "colep_ai.worker.tasks.ingest.*": {
            "queue": "ingestion",
            "routing_key": "ingestion",
        },
        "colep_ai.worker.tasks.index.*": {
            "queue": "indexing",
            "routing_key": "indexing",
        },
        "colep_ai.worker.tasks.cleanup.*": {
            "queue": "cleanup",
            "routing_key": "cleanup",
        },
    },

    # ── Reliability ────────────────────────────────────────────────────────
    # task_acks_late: message is NOT acknowledged until task completes.
    # If worker crashes mid-task, broker requeues the message automatically.
    task_acks_late=True,

    # task_reject_on_worker_lost: explicitly rejects (not acks) the message
    # when the worker process is killed, ensuring requeue even on hard crash.
    task_reject_on_worker_lost=True,

    # ── Serialization ──────────────────────────────────────────────────────
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # ── Result backend ─────────────────────────────────────────────────────
    # Results expire after 7 days — same as tracker TTL.
    result_expires=7 * 24 * 60 * 60,

    # ── Timezone ───────────────────────────────────────────────────────────
    timezone="UTC",
    enable_utc=True,
)