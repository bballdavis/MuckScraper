"""A small job queue in Postgres for background work.

Web requests used to start long admin actions in threads inside a gunicorn
worker, where a worker timeout or restart killed them mid-run. Now a request
adds a row to `jobs` (enqueue) and the worker thread in the scheduler
container claims it with SELECT ... FOR UPDATE SKIP LOCKED and runs it. A
NOTIFY on commit wakes the worker at once; it also polls, so a lost
notification only delays a job by POLL_SECONDS.

Handlers are registered by kind (register()); aggregator/blueprints/admin/
_tasks.py registers the admin bulk actions and AI tasks, aggregator/
newsbox.py the app's NewsBox summaries, aggregator/breaking.py Breaking
News scrapes. One worker runs one
job at a time, highest priority first, then oldest first.
"""
import json
import logging
import select
import threading

from aggregator.models import db, Job

logger = logging.getLogger(__name__)

CHANNEL = "jobs"
POLL_SECONDS = 30
INTERRUPTED_MESSAGE = "Interrupted by a scheduler restart; run it again."

# kind -> (run(payload), fail(payload, message) or None)
_HANDLERS = {}

# Jobs only the capture container can run (it has the headless browser);
# the scheduler's worker leaves them queued for it.
BROWSER_KINDS = ("bookmark_capture",)


def register(kind, run, fail=None):
    """Register `run(payload)` for jobs of `kind`. `fail(payload, message)`,
    if given, is called when the job errors or is interrupted, so the caller's
    own status display can say so."""
    _HANDLERS[kind] = (run, fail)


def enqueue(kind, payload=None, priority=0):
    """Queue a job and commit. Returns the job id."""
    job = Job(kind=kind, payload=json.dumps(payload or {}), priority=priority)
    db.session.add(job)
    db.session.flush()
    # Delivered when the transaction commits, so the worker never sees a job
    # before it exists.
    db.session.execute(db.text("SELECT pg_notify(:channel, '')"), {"channel": CHANNEL})
    db.session.commit()
    return job.id


def _kind_filter(kinds, exclude):
    """SQL and params limiting a claim to `kinds`, or to all but `exclude`."""
    if kinds:
        return "AND kind = ANY(:kinds)", {"kinds": list(kinds)}
    if exclude:
        return "AND kind <> ALL(:exclude)", {"exclude": list(exclude)}
    return "", {}


def _claim_next(kinds=None, exclude=None):
    """Mark the next queued job running and return (id, kind, payload), or
    None when the queue is empty. `kinds` limits it to those kinds;
    `exclude` skips those."""
    where, params = _kind_filter(kinds, exclude)
    row = db.session.execute(db.text(
        f"""
        UPDATE jobs SET status = 'running', started_at = (now() at time zone 'utc')
        WHERE id = (
            SELECT id FROM jobs WHERE status = 'queued' {where}
            ORDER BY priority DESC, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, kind, payload
        """
    ), params).fetchone()
    db.session.commit()
    if row is None:
        return None
    return row.id, row.kind, json.loads(row.payload or "{}")


def _finish(job_id, error=None):
    db.session.execute(
        db.text(
            "UPDATE jobs SET status = :status, finished_at = (now() at time zone 'utc'), error = :error "
            "WHERE id = :id"
        ),
        {"status": "error" if error else "done", "error": error, "id": job_id},
    )
    db.session.commit()


def _call_fail(fail, payload, message):
    if fail is None:
        return
    try:
        fail(payload, message)
    except Exception:
        db.session.rollback()
        logger.exception("[Jobs] fail handler raised")


def run_next_job(kinds=None, exclude=BROWSER_KINDS):
    """Run one queued job, if any. Returns True when a job was run."""
    claimed = _claim_next(kinds, exclude)
    if claimed is None:
        return False
    job_id, kind, payload = claimed
    run, fail = _HANDLERS.get(kind, (None, None))
    if run is None:
        logger.error("[Jobs] No handler for job %s kind=%s", job_id, kind)
        _finish(job_id, error=f"No handler for kind {kind!r}")
        return True
    logger.info("[Jobs] Running job %s kind=%s", job_id, kind)
    try:
        run(payload)
    except Exception as exc:
        db.session.rollback()
        logger.exception("[Jobs] Job %s kind=%s failed", job_id, kind)
        _call_fail(fail, payload, str(exc))
        _finish(job_id, error=str(exc)[:2000])
    else:
        _finish(job_id)
        logger.info("[Jobs] Job %s kind=%s done", job_id, kind)
    finally:
        db.session.remove()
    return True


def fail_interrupted_jobs(kinds=None, exclude=BROWSER_KINDS):
    """Mark jobs left 'running' by a previous worker process as errors. One
    worker runs each set of kinds, so at its start any of its running jobs
    is an orphan."""
    where, params = _kind_filter(kinds, exclude)
    rows = db.session.execute(db.text(
        f"""
        UPDATE jobs SET status = 'error', finished_at = (now() at time zone 'utc'), error = :msg
        WHERE status = 'running' {where}
        RETURNING kind, payload
        """
    ), {"msg": INTERRUPTED_MESSAGE, **params}).fetchall()
    db.session.commit()
    for row in rows:
        _, fail = _HANDLERS.get(row.kind, (None, None))
        _call_fail(fail, json.loads(row.payload or "{}"), INTERRUPTED_MESSAGE)
    if rows:
        logger.warning("[Jobs] Marked %s interrupted job(s) as failed", len(rows))
    return len(rows)


def _listen_connection():
    """A pooled connection checked out for good, in autocommit so LISTEN
    takes effect at once. Returns (pool proxy, psycopg2 connection)."""
    proxy = db.engine.raw_connection()
    conn = proxy.connection
    conn.autocommit = True
    conn.cursor().execute(f"LISTEN {CHANNEL}")
    return proxy, conn


def _wait_for_notify(conn, timeout):
    if select.select([conn], [], [], timeout) != ([], [], []):
        conn.poll()
        conn.notifies.clear()


def worker_loop(app, stop_event=None, kinds=None):
    """Run jobs until `stop_event` is set: drain the queue, then sleep until
    a NOTIFY or POLL_SECONDS. Survives database outages by retrying.
    `kinds` makes it run only those (the capture container); otherwise it
    runs everything but BROWSER_KINDS."""
    stop_event = stop_event or threading.Event()
    exclude = None if kinds else BROWSER_KINDS
    with app.app_context():
        if not kinds:
            # Registers the admin handlers (bulk actions, AI tasks), the
            # app's NewsBox summaries, bookmark snapshots and article jobs.
            import aggregator.blueprints.admin._tasks  # noqa: F401
            import aggregator.newsbox  # noqa: F401
            import aggregator.bookmarks  # noqa: F401
            import aggregator.breaking  # noqa: F401
            import aggregator.article_jobs  # noqa: F401
        fail_interrupted_jobs(kinds, exclude)
        listen = None
        while not stop_event.is_set():
            try:
                if listen is None:
                    listen = _listen_connection()
                while not stop_event.is_set() and run_next_job(kinds, exclude):
                    pass
                _wait_for_notify(listen[1], POLL_SECONDS)
            except Exception:
                logger.exception("[Jobs] Worker loop error; retrying shortly")
                db.session.rollback()
                if listen is not None:
                    try:
                        listen[0].invalidate()
                    except Exception:
                        pass
                    listen = None
                stop_event.wait(POLL_SECONDS)


def start_worker_thread(app):
    """Start the worker as a daemon thread (the scheduler container's main
    thread runs APScheduler). Returns (thread, stop_event)."""
    stop_event = threading.Event()
    thread = threading.Thread(target=worker_loop, args=(app, stop_event), name="job-worker", daemon=True)
    thread.start()
    return thread, stop_event


def queue_counts():
    """{'queued': n, 'running': n} for status displays."""
    rows = db.session.execute(db.text(
        "SELECT status, count(*) FROM jobs WHERE status IN ('queued', 'running') GROUP BY status"
    )).fetchall()
    counts = {"queued": 0, "running": 0}
    counts.update({status: n for status, n in rows})
    return counts


def prune_finished(days=30):
    """Delete finished jobs older than `days`."""
    result = db.session.execute(db.text(
        "DELETE FROM jobs WHERE status IN ('done', 'error') "
        "AND finished_at < (now() at time zone 'utc') - make_interval(days => :d)"
    ), {"d": days})
    db.session.commit()
    return result.rowcount

