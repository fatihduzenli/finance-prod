"""In-process queue and its one worker thread (docs/qa/PLAN-008-webhooks.md D1).

The receiver enqueues the ID of each new event after its transaction commits. The
worker thread is started on the first enqueue in a process, is a daemon, and takes one
ID at a time: each event is processed in its own transaction by
:func:`payments.webhooks.processor.process_event`, then the thread's database
connections are closed. An event that fails is not put back: it waits for a replay or
the next ``process_webhooks`` run, which also catches up on whatever was queued when a
process stopped (the queue lives in memory only).
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid

from django.db import connections

from payments.webhooks import processor

THREAD_NAME = "webhook-worker"

logger = logging.getLogger(__name__)

_pending: queue.SimpleQueue[uuid.UUID] = queue.SimpleQueue()
_worker: threading.Thread | None = None
_lock = threading.Lock()


def enqueue(event_id: uuid.UUID) -> None:
    """Hand one stored event to the worker thread, starting it if needed."""
    _ensure_worker()
    _pending.put(event_id)


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name=THREAD_NAME, daemon=True)
            _worker.start()


def _work() -> None:
    while True:
        try:
            process_one(_pending.get())
        finally:
            # This thread's connections only; a broken one is never reused.
            connections.close_all()


def process_one(event_id: uuid.UUID) -> None:
    """Process one event; never raises, so the thread keeps running."""
    try:
        processor.process_event(event_id)
    except Exception as error:  # the thread must survive any failure; class name only
        logger.error(
            "Webhook worker could not process event %s: %s", event_id, type(error).__name__
        )
