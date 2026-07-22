# app/tracking/worker_heartbeat.py
import os
import json
import time
import threading
from celery.signals import worker_ready, worker_shutting_down

import redis
from colep_ai.core.config import settings

_redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

HEARTBEAT_KEY_PREFIX = "worker:hb:"
HEARTBEAT_INTERVAL_S = 5
HEARTBEAT_TTL_S = 20  # 4x interval — stale if missed 4 consecutive beats

_stop_event = threading.Event()


def _heartbeat_loop(hostname: str) -> None:
    pid = os.getpid()
    key = f"{HEARTBEAT_KEY_PREFIX}{hostname}"
    while not _stop_event.is_set():
        payload = json.dumps({"pid": pid, "ts": time.time()})
        _redis.set(key, payload, ex=HEARTBEAT_TTL_S)
        _stop_event.wait(HEARTBEAT_INTERVAL_S)

@worker_ready.connect
def start_heartbeat(sender=None, **kwargs):
    _stop_event.clear()
    t = threading.Thread(
        target=_heartbeat_loop,
        args=(sender.hostname,),
        daemon=True,
        name="worker-heartbeat",
    )
    t.start()


@worker_shutting_down.connect
def stop_heartbeat(**kwargs):
    _stop_event.set()