"""A small job queue in Postgres for background work.

Web requests used to start long admin actions in threads inside a gunicorn
worker, where a worker timeout or restart killed them mid-run. Now a request
adds a row to `jobs` (enqueue) and the worker thread in the scheduler
container claims it with SELECT ... FOR UPDATE SKIP LOCKED and runs it. A
NOTIFY on commit wakes the worker at once; it also polls, so a lost
notification only delays a job by POLL_SECONDS.

Handlers are registered by kind (register()); aggregator/blueprints/admin/
_tasks.py registers the admin bulk actions and AI tasks. One worker runs one
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


def _claim_next():
    """Mark the next queued job running and return (id, kind, payload), or
    None when the queue is empty."""
    row = db.session.execute(db.text(
        """
        UPDATE jobs SET status = 'running', started_at = (now() at time zone 'utc')
        WHERE id = (
            SELECT id FROM jobs WHERE status = 'queued'
            ORDER BY priority DESC, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, kind, payload
        """
    )).fetchone()
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


def run_next_job():
    """Run one queued job, if any. Returns True when a job was run."""
    claimed = _claim_next()
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


def fail_interrupted_jobs():
    """Mark jobs left 'running' by a previous worker process as errors. Only
    one worker exists, so at its start any running job is an orphan."""
    rows = db.session.execute(db.text(
        """
        UPDATE jobs SET status = 'error', finished_at = (now() at time zone 'utc'), error = :msg
        WHERE status = 'running'
        RETURNING kind, payload
        """
    ), {"msg": INTERRUPTED_MESSAGE}).fetchall()
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


def worker_loop(app, stop_event=None):
    """Run jobs until `stop_event` is set: drain the queue, then sleep until
    a NOTIFY or POLL_SECONDS. Survives database outages by retrying."""
    stop_event = stop_event or threading.Event()
    with app.app_context():
        # Registers the admin handlers (bulk actions, AI tasks).
        import aggregator.blueprints.admin._tasks  # noqa: F401
        fail_interrupted_jobs()
        listen = None
        while not stop_event.is_set():
            try:
                if listen is None:
                    listen = _listen_connection()
                while not stop_event.is_set() and run_next_job():
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

