"""What the scheduler container is doing, for Settings › Pipeline › Tasks.

The scheduler (news_fetcher/scheduler.py) runs its recurring tasks on
APScheduler threads and the job queue (aggregator/jobs.py) on a worker
thread. It records here, in app_settings rows, each task's last run and a
heartbeat with its next run times. The web app reads them for the Tasks page
and writes controls back (pause, run now), which the scheduler picks up on
its next control tick (CONTROL_SECONDS).

Writes go through their own connection, not db.session, so recording a step
in the middle of a pipeline run never commits the run's half-done work.
"""
import json
from datetime import datetime, timedelta

from aggregator.models import AppSetting, db

STATE_KEY = "scheduler_state_v1"
RUN_PREFIX = "task_run_v1:"
PAUSE_PREFIX = "task_paused_v1:"
RUN_NOW_PREFIX = "task_run_now_v1:"

# How often the scheduler applies controls and the schedule, and writes its heartbeat.
CONTROL_SECONDS = 15
# A heartbeat older than this means the scheduler isn't running.
STALE_AFTER = timedelta(minutes=2)
# A pipeline step with no progress for this long is shown as slow. The
# watchdog (aggregator/workers.py) requeues a job whose heartbeat stops.
STEP_STUCK_AFTER = timedelta(minutes=20)

# Tasks the page lists, in order: id -> (name, what it does). Pipeline hours
# are "pipeline_schedule_<hour>"; "pipeline_full" and "pipeline_fetch" are
# the Run now buttons for a whole run.
TASKS = {
    "pipeline_full": ("Full pipeline", "Fetch, enrich, headlines, publish the edition"),
    "pipeline_fetch": ("Fetch only", "Topic and RSS fetches, scrape, group, card images"),
    "breaking_feeds": ("Breaking News feeds", "Read the general feeds' headlines and flag breaking events"),
    "image_focus": ("Photo focal points", "Find where to crop new story photos"),
    "nyt_wire": ("NYT Times Wire", "Poll the Times Wire on its own schedule, within the daily NYT budget"),
    "newsbox_schedules": ("NewsBox schedules", "Queue each reader's scheduled summaries when they're due"),
}
# Recurring tasks that can't be paused from the page (the page itself needs them).
UNPAUSABLE = {"control"}


def _now():
    return datetime.utcnow()


def _iso(value):
    return value.isoformat() if value else None


def _parse(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def write_json(key, value):
    """Upsert (or, with None, delete) one app_settings JSON row on its own connection."""
    payload = None if value is None else json.dumps(value, sort_keys=True, default=str)
    with db.engine.begin() as conn:
        if payload is None:
            conn.execute(db.text("DELETE FROM app_settings WHERE key = :key"), {"key": key})
        else:
            conn.execute(db.text(
                "INSERT INTO app_settings (key, value) VALUES (:key, :value) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ), {"key": key, "value": payload})


def _read_prefix(prefix):
    rows = AppSetting.query.filter(AppSetting.key.like(prefix + "%")).all()
    out = {}
    for row in rows:
        try:
            out[row.key[len(prefix):]] = json.loads(row.value) if row.value else None
        except (TypeError, ValueError):
            continue
    return out


def _read(key):
    row = AppSetting.query.filter_by(key=key).first()
    try:
        return json.loads(row.value) if row and row.value else None
    except (TypeError, ValueError):
        return None


# Written by the scheduler ------------------------------------------------

def record_start(task_id):
    previous = _read(RUN_PREFIX + task_id) or {}
    write_json(RUN_PREFIX + task_id, {**previous, "running_since": _iso(_now())})


def record_run(task_id, started_at, finished_at, status, error=None, summary=None):
    """A recurring task's last run: when, how long, and what it reported."""
    write_json(RUN_PREFIX + task_id, {
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "seconds": round((finished_at - started_at).total_seconds(), 1),
        "status": status,
        "error": (error or "")[:500] or None,
        "summary": _short_summary(summary),
    })


def _short_summary(summary):
    """The small numbers a task returns (counts), not nested detail."""
    if not isinstance(summary, dict):
        return None
    return {k: v for k, v in summary.items() if isinstance(v, (int, float, str, bool)) and len(str(v)) < 80} or None


def write_state(started_at, jobs, worker_alive, schedule_loaded_at, workers=None, restarted=0, requeued=0):
    """The heartbeat: every scheduled task's next run, whether the queue
    worker threads are all up, each thread's job, and how often the
    watchdog has restarted a thread or requeued a job since start."""
    write_json(STATE_KEY, {
        "heartbeat_at": _iso(_now()),
        "started_at": _iso(started_at),
        "worker_alive": worker_alive,
        "schedule_loaded_at": _iso(schedule_loaded_at),
        "jobs": jobs,
        "workers": workers or [],
        "watchdog": {"restarted": restarted, "requeued": requeued},
    })


def paused_ids():
    return {task_id for task_id, value in _read_prefix(PAUSE_PREFIX).items() if value}


def take_run_requests():
    """Run now requests, oldest first, removed as they're taken."""
    requests = _read_prefix(RUN_NOW_PREFIX)
    for task_id in requests:
        write_json(RUN_NOW_PREFIX + task_id, None)
    return sorted(requests, key=lambda t: (requests[t] or {}).get("requested_at") or "")


# Written by the web app --------------------------------------------------

def set_paused(task_id, paused):
    write_json(PAUSE_PREFIX + task_id, {"paused_at": _iso(_now())} if paused else None)


def request_run(task_id, user=None):
    write_json(RUN_NOW_PREFIX + task_id, {"requested_at": _iso(_now()), "by": user})


# Read by the web app -----------------------------------------------------

def state():
    """The scheduler's last heartbeat, with `alive` (recent enough) added."""
    data = _read(STATE_KEY) or {}
    beat = _parse(data.get("heartbeat_at"))
    data["alive"] = bool(beat and _now() - beat < STALE_AFTER)
    data["heartbeat_age"] = (_now() - beat).total_seconds() if beat else None
    return data


def runs():
    return _read_prefix(RUN_PREFIX)


def pending_run_requests():
    return set(_read_prefix(RUN_NOW_PREFIX))


def schedule_applied_since(changed_at_iso):
    """Whether the running scheduler has loaded the schedule since this edit."""
    data = state()
    loaded = data.get("schedule_loaded_at")
    return bool(data["alive"] and loaded and changed_at_iso and loaded >= changed_at_iso)
