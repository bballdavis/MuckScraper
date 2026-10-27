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
News scrapes, news_fetcher/pipeline_run.py the pipeline's stage, feed and
story jobs. Each worker thread runs one job at a time, highest priority
first, then oldest first; aggregator/workers.py runs several, each on its
own kinds.

A running job keeps a heartbeat (beat()). A kind registered with `retry`
is put back in the queue, up to MAX_ATTEMPTS, when a restart interrupts it
or the watchdog finds its heartbeat stopped; each claim raises `attempts`,
and a finish only counts for the attempt that claimed the job.
"""
import json
import logging
import select
import threading
import time

from aggregator.models import db, Job

logger = logging.getLogger(__name__)

CHANNEL = "jobs"
POLL_SECONDS = 30
INTERRUPTED_MESSAGE = "Interrupted by a scheduler restart; run it again."
# Claims of one retryable job before it is failed instead of requeued.
MAX_ATTEMPTS = 3
# How often beat() writes the heartbeat; calls in between are free.
BEAT_SECONDS = 20

# kind -> (run(payload), fail(payload, message) or None)
_HANDLERS = {}
# kind -> {"retry": bool, "stuck_after": timedelta or None, "after": callable or None}
_OPTIONS = {}
# The job each worker thread is running: .job = (id, kind, attempts), .beat_at.
_current = threading.local()
# The same, by thread name, for the watchdog (aggregator/workers.py).
running_by_thread = {}

# Jobs only the capture container can run (it has the headless browser);
# the scheduler's worker leaves them queued for it.
BROWSER_KINDS = ("bookmark_capture",)


def register(kind, run, fail=None, retry=False, stuck_after=None, after=None):
    """Register `run(payload)` for jobs of `kind`. `fail(payload, message)`,
    if given, is called when the job errors or is interrupted, so the caller's
    own status display can say so. What `run` returns, if it is a dict, is
    kept in the job's `result`.

    `retry`: put the job back in the queue when a restart interrupts it
    (else it fails). `stuck_after` (a timedelta): the watchdog requeues it,
    or fails it after MAX_ATTEMPTS, when its heartbeat is older than that,
    so its handler must call beat() at least that often. `after(payload)`
    is called once a run's success is recorded."""
    _HANDLERS[kind] = (run, fail)
    _OPTIONS[kind] = {"retry": retry, "stuck_after": stuck_after, "after": after}


def options(kind):
    return _OPTIONS.get(kind) or {"retry": False, "stuck_after": None, "after": None}


def enqueue(kind, payload=None, priority=0, run_id=None):
    """Queue a job and commit. Returns the job id."""
    job = Job(kind=kind, payload=json.dumps(payload or {}), priority=priority, run_id=run_id)
    db.session.add(job)
    db.session.flush()
    # Delivered when the transaction commits, so the worker never sees a job
    # before it exists.
    db.session.execute(db.text("SELECT pg_notify(:channel, '')"), {"channel": CHANNEL})
    db.session.commit()
    return job.id


def enqueue_many(kind, payloads, priority=0, run_id=None):
    """Queue several jobs of one kind in one commit. Returns their ids."""
    rows = [Job(kind=kind, payload=json.dumps(p or {}), priority=priority, run_id=run_id) for p in payloads]
    db.session.add_all(rows)
    db.session.flush()
    db.session.execute(db.text("SELECT pg_notify(:channel, '')"), {"channel": CHANNEL})
    db.session.commit()
    return [row.id for row in rows]


def beat():
    """Say the current job is still making progress. Cheap to call often:
    the heartbeat is written at most every BEAT_SECONDS, on its own
    connection so it never commits the job's half-done work. A no-op
    outside a job."""
    job = getattr(_current, "job", None)
    if job is None:
        return
    now = time.monotonic()
    if now - getattr(_current, "beat_at", 0) < BEAT_SECONDS:
        return
    _current.beat_at = now
    try:
        with db.engine.begin() as conn:
            conn.execute(db.text(
                "UPDATE jobs SET heartbeat_at = (now() at time zone 'utc') "
                "WHERE id = :id AND attempts = :attempts AND status = 'running'"
            ), {"id": job[0], "attempts": job[2]})
    except Exception:
        logger.exception("[Jobs] Could not write the heartbeat for job %s", job[0])


def current_job_id():
    job = getattr(_current, "job", None)
    return job[0] if job else None


def ensure_schema():
    """Add the columns the stage jobs need when the migration hasn't run
    yet (create_all() makes new tables, not new columns)."""
    with db.engine.begin() as conn:
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITHOUT TIME ZONE",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_id INTEGER",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result TEXT",
            "CREATE INDEX IF NOT EXISTS ix_jobs_run_id ON jobs (run_id)",
        ):
            conn.execute(db.text(ddl))


def _kind_filter(kinds, exclude):
    """SQL and params limiting a claim to `kinds`, or to all but `exclude`."""
    if kinds:
        return "AND kind = ANY(:kinds)", {"kinds": list(kinds)}
    if exclude:
        return "AND kind <> ALL(:exclude)", {"exclude": list(exclude)}
    return "", {}


def _claim_next(kinds=None, exclude=None):
    """Mark the next queued job running and return (id, kind, payload,
    attempts), or None when the queue is empty. `kinds` limits it to those
    kinds; `exclude` skips those."""
    where, params = _kind_filter(kinds, exclude)
    row = db.session.execute(db.text(
        f"""
        UPDATE jobs SET status = 'running', started_at = (now() at time zone 'utc'),
            heartbeat_at = (now() at time zone 'utc'), attempts = attempts + 1
        WHERE id = (
            SELECT id FROM jobs WHERE status = 'queued' {where}
            ORDER BY priority DESC, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, kind, payload, attempts
        """
    ), params).fetchone()
    db.session.commit()
    if row is None:
        return None
    return row.id, row.kind, json.loads(row.payload or "{}"), row.attempts


def _finish(job_id, error=None, attempts=None, result=None):
    """Record the outcome. With `attempts`, only if the job is still the
    claim that ran it (not requeued by the watchdog since). Returns whether
    it was recorded."""
    guard = " AND attempts = :attempts AND status = 'running'" if attempts is not None else ""
    updated = db.session.execute(
        db.text(
            "UPDATE jobs SET status = :status, finished_at = (now() at time zone 'utc'), error = :error, "
            "result = :result "
            "WHERE id = :id" + guard
        ),
        {"status": "error" if error else "done", "error": error, "id": job_id, "attempts": attempts,
         "result": json.dumps(result, default=str) if isinstance(result, dict) else None},
    ).rowcount
    db.session.commit()
    return bool(updated)


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
    job_id, kind, payload, attempts = claimed
    run, fail = _HANDLERS.get(kind, (None, None))
    if run is None:
        logger.error("[Jobs] No handler for job %s kind=%s", job_id, kind)
        _finish(job_id, error=f"No handler for kind {kind!r}")
        return True
    logger.info("[Jobs] Running job %s kind=%s", job_id, kind)
    _current.job, _current.beat_at = (job_id, kind, attempts), time.monotonic()
    running_by_thread[threading.current_thread().name] = _current.job
    try:
        result = run(payload)
    except Exception as exc:
        db.session.rollback()
        logger.exception("[Jobs] Job %s kind=%s failed", job_id, kind)
        if _finish(job_id, error=str(exc)[:2000], attempts=attempts):
            _call_fail(fail, payload, str(exc))
    else:
        if _finish(job_id, attempts=attempts, result=result):
            logger.info("[Jobs] Job %s kind=%s done", job_id, kind)
            after = options(kind)["after"]
            if after is not None:
                try:
                    after(payload)
                except Exception:
                    db.session.rollback()
                    logger.exception("[Jobs] after handler for job %s raised", job_id)
        else:
            logger.warning("[Jobs] Job %s kind=%s finished after it was requeued; result dropped", job_id, kind)
    finally:
        _current.job = None
        running_by_thread.pop(threading.current_thread().name, None)
        db.session.remove()
    return True


def requeue_or_fail(job_id, message):
    """Put a running job back in the queue if its kind retries and it has
    tries left, else fail it. Returns 'requeued', 'failed' or None (it
    wasn't running)."""
    row = db.session.execute(db.text(
        "SELECT kind, payload, attempts FROM jobs WHERE id = :id AND status = 'running'"
    ), {"id": job_id}).fetchone()
    if row is None:
        db.session.rollback()
        return None
    if options(row.kind)["retry"] and row.attempts < MAX_ATTEMPTS:
        db.session.execute(db.text(
            "UPDATE jobs SET status = 'queued', started_at = NULL, heartbeat_at = NULL, error = :msg "
            "WHERE id = :id AND status = 'running'"
        ), {"id": job_id, "msg": message})
        db.session.execute(db.text("SELECT pg_notify(:channel, '')"), {"channel": CHANNEL})
        db.session.commit()
        logger.warning("[Jobs] Requeued job %s kind=%s (%s)", job_id, row.kind, message)
        return "requeued"
    db.session.execute(db.text(
        "UPDATE jobs SET status = 'error', finished_at = (now() at time zone 'utc'), error = :msg "
        "WHERE id = :id AND status = 'running'"
    ), {"id": job_id, "msg": message})
    db.session.commit()
    _, fail = _HANDLERS.get(row.kind, (None, None))
    _call_fail(fail, json.loads(row.payload or "{}"), message)
    logger.warning("[Jobs] Failed job %s kind=%s (%s)", job_id, row.kind, message)
    return "failed"


def fail_interrupted_jobs(kinds=None, exclude=BROWSER_KINDS):
    """Deal with jobs left 'running' by a previous worker process: a kind
    registered with `retry` goes back in the queue (a pipeline run resumes
    at the stage it was on), the rest are marked as errors. Call it once,
    before this process's workers start: at that point any of its running
    jobs is an orphan."""
    where, params = _kind_filter(kinds, exclude)
    rows = db.session.execute(db.text(
        f"SELECT id FROM jobs WHERE status = 'running' {where}"
    ), params).fetchall()
    db.session.commit()
    outcomes = [requeue_or_fail(row.id, INTERRUPTED_MESSAGE) for row in rows]
    if rows:
        logger.warning("[Jobs] %s interrupted job(s): %s requeued, %s failed", len(rows),
                       outcomes.count("requeued"), outcomes.count("failed"))
    return len(rows)


def stuck_jobs():
    """Running jobs whose kind has a `stuck_after` and whose heartbeat is
    older than it: [(id, kind, minutes since the heartbeat)]."""
    kinds = {kind: o["stuck_after"] for kind, o in _OPTIONS.items() if o.get("stuck_after")}
    if not kinds:
        return []
    rows = db.session.execute(db.text(
        "SELECT id, kind, EXTRACT(EPOCH FROM (now() at time zone 'utc') - COALESCE(heartbeat_at, started_at)) AS age "
        "FROM jobs WHERE status = 'running' AND kind = ANY(:kinds)"
    ), {"kinds": list(kinds)}).fetchall()
    db.session.commit()
    return [(row.id, row.kind, row.age / 60) for row in rows
            if row.age is not None and row.age > kinds[row.kind].total_seconds()]


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


def register_handlers():
    """Import the modules that register handlers: the admin bulk actions and
    AI tasks, the app's NewsBox summaries, bookmark snapshots, Breaking
    scrapes and article jobs."""
    import aggregator.blueprints.admin._tasks  # noqa: F401
    import aggregator.newsbox  # noqa: F401
    import aggregator.bookmarks  # noqa: F401
    import aggregator.breaking  # noqa: F401
    import aggregator.article_jobs  # noqa: F401
    # The pipeline's stage, feed and story jobs register in
    # news_fetcher/pipeline_run.py, which the scheduler imports.


def cancel(job_id, message="Cancelled from Settings › Tasks."):
    """Cancel a queued job, letting its fail handler clean up (a NewsBox
    item shows as failed rather than pending forever). Returns False when it
    isn't queued any more."""
    row = db.session.execute(db.text(
        "UPDATE jobs SET status = 'cancelled', finished_at = (now() at time zone 'utc'), error = :error "
        "WHERE id = :id AND status = 'queued' RETURNING kind, payload"
    ), {"id": job_id, "error": message}).fetchone()
    db.session.commit()
    if row is None:
        return False
    register_handlers()
    _, fail = _HANDLERS.get(row.kind, (None, None))
    _call_fail(fail, json.loads(row.payload or "{}"), message)
    return True


def worker_loop(app, stop_event=None, kinds=None, exclude=None, recover=True):
    """Run jobs until `stop_event` is set: drain the queue, then sleep until
    a NOTIFY or POLL_SECONDS. Survives database outages by retrying.
    `kinds` makes it run only those (the capture container); otherwise it
    runs everything but BROWSER_KINDS and `exclude`. `recover` deals with
    jobs a previous process left running first; aggregator/workers.py does
    that once for all its threads and passes False."""
    stop_event = stop_event or threading.Event()
    if not kinds:
        exclude = tuple(BROWSER_KINDS) + tuple(exclude or ())
    else:
        exclude = None
    with app.app_context():
        if not kinds:
            register_handlers()
        if recover:
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

