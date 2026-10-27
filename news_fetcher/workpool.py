"""A shared thread pool for the network-bound parts of ingestion.

Scraping a page, classifying a title and the like are almost all waiting
on the network, so running them side by side is where a pipeline run's time
goes back (news_fetcher/fetch_and_store_articles/ingestion.py). One pool for
the whole process, so the several feed jobs running at once share
INGEST_THREADS threads rather than each starting its own.

Each task runs in its own app context with its own database session,
removed when it ends.
"""
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import current_app, has_app_context

logger = logging.getLogger(__name__)

INGEST_THREADS = max(1, int(os.getenv("INGEST_THREADS", "12")))

_executor = None
_executor_lock = threading.Lock()


def _pool():
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=INGEST_THREADS, thread_name_prefix="ingest-pool")
        return _executor


def _in_app(app, fn):
    def call(item):
        from aggregator import db
        with app.app_context():
            try:
                return fn(item)
            finally:
                db.session.remove()
    return call


def map_in_app(fn, items, on_each=None):
    """[fn(item) for item in items], run on the shared pool, in order. An
    exception in one call is raised here, as the loop would. `on_each()` is
    called on this thread as each call finishes (a job's heartbeat).

    Runs inline with one thread, one item or no app context, so nothing
    changes for a caller outside the scheduler."""
    items = list(items)
    if INGEST_THREADS <= 1 or len(items) <= 1 or not has_app_context():
        results = []
        for item in items:
            results.append(fn(item))
            if on_each:
                on_each()
        return results
    call = _in_app(current_app._get_current_object(), fn)
    futures = [_pool().submit(call, item) for item in items]
    for _ in as_completed(futures):
        if on_each:
            on_each()
    return [future.result() for future in futures]
