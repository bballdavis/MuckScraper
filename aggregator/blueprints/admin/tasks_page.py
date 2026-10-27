"""Settings › Pipeline › Tasks: what the scheduler is running now, its
recurring tasks with Run now and Pause, recent pipeline runs with where the
time went, the job queue, and the one-off jobs (moved here from Overview).

The scheduler writes what it's doing to app_settings (aggregator/
scheduler_tasks.py); this page reads that and writes Pause / Run now back,
which the scheduler applies within CONTROL_SECONDS.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import render_template, request, redirect, url_for
from flask_login import current_user

from aggregator import jobs, scheduler_tasks
from aggregator.models import Job, PipelineSchedule
from aggregator.permissions import ROLE_ADMIN, role_required

from . import admin, FETCH_RUN_STATUS_KEY
from ._shared import _load_json_setting
from .tools import OVERVIEW_JOBS, _running_operations

TIMEZONE = ZoneInfo("America/New_York")
SCRAPE_OUTCOME_HISTORY_KEY = "scrape_outcome_history_v1"
HISTORY_ROWS = 10

# The pipeline's steps (scheduler._mark_step labels) in four stages, for the
# run history bars.
STAGES = (
    ("fetch", "Fetch", ("Fetching", "Ingesting NYT", "Filling missing card images")),
    ("enrich", "Enrich", ("Retrying unrated", "Syncing AllSides", "Running headline ranking", "Targeted RSS")),
    ("stories", "Stories", ("Marking copied", "Generating headlines")),
    ("publish", "Publish", ("Publishing", "Processing current edition", "Linking Op/Ed", "Daily horoscope", "Exporting")),
)

JOB_LABELS = {
    "newsbox_summary": "NewsBox summary",
    "ai_task": "AI task",
    "bulk_task": "Bulk action",
    "article_summary": "Article summary",
    "article_bias": "Article lean rating",
    "bookmark_snapshot": "Saved story snapshot",
    "bookmark_capture": "Saved story capture",
    "bookmark_delete": "Saved story delete",
    "scrape_feed_entry": "Breaking News scrape",
}


def _stage_of(step):
    for key, _, prefixes in STAGES:
        if any(step.startswith(p) for p in prefixes):
            return key
    return "fetch"


def _local(value):
    """A naive UTC datetime or ISO string, in New York time."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value is None:
        return None
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(TIMEZONE)


def _when(value):
    """'3:05 PM' today, 'Yesterday 3:05 PM', else 'Oct 24 3:05 PM'."""
    at = _local(value)
    if at is None:
        return None
    today = datetime.now(TIMEZONE).date()
    clock = at.strftime("%-I:%M %p")
    if at.date() == today:
        return clock
    if at.date() == today - timedelta(days=1):
        return f"Yesterday {clock}"
    return at.strftime("%b %-d ") + clock


def _span(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def _ago(value):
    at = _local(value)
    if at is None:
        return None
    return _span((datetime.now(TIMEZONE) - at).total_seconds())


def _summary_text(summary):
    """A task's own counts, as a short phrase."""
    if not summary:
        return None
    if "feeds" in summary:
        return f"{summary.get('new', 0)} new, {summary.get('breaking', 0)} breaking"
    parts = [f"{v} {k.replace('_', ' ')}" for k, v in summary.items() if isinstance(v, int) and v]
    return ", ".join(parts[:2]) or None


def _hour_label(hour):
    return datetime(2000, 1, 1, hour).strftime("%-I %p")


def _running_now():
    status = _load_json_setting(FETCH_RUN_STATUS_KEY) or {}
    if status.get("status") != "running":
        return None
    timings = status.get("timings") or []
    step_age = None
    if status.get("step_started_at"):
        step_age = (datetime.utcnow() - datetime.fromisoformat(status["step_started_at"])).total_seconds()
    stages = []
    current = _stage_of(status["step"]) if status.get("step") else None
    seen = {_stage_of(t["step"]) for t in timings}
    order = [key for key, _, _ in STAGES]
    for key, label, _ in STAGES:
        seconds = sum(t.get("seconds") or 0 for t in timings if _stage_of(t["step"]) == key)
        if key == current:
            state = "run"
        elif key in seen or (current and order.index(key) < order.index(current)):
            state = "done"
        else:
            state = "todo"
        if not status.get("run_full_pipeline") and key != "fetch":
            state = "skip"
        stages.append({"label": label, "state": state, "time": _span(seconds) if seconds else None})
    return {
        "kind": "Full pipeline" if status.get("run_full_pipeline") else "Fetch only",
        "started": _when(status.get("started_at")),
        "elapsed": _ago(status.get("started_at")),
        "step": status.get("step"),
        "step_elapsed": _span(step_age) if step_age is not None else None,
        "slow": step_age is not None and step_age > scheduler_tasks.STEP_STUCK_AFTER.total_seconds(),
        "stages": stages,
    }


def _recurring(state, runs, paused, requested):
    next_by_id = {j["id"]: j.get("next_run_at") for j in state.get("jobs") or []}
    schedule = PipelineSchedule.query.filter_by(is_active=True).order_by(PipelineSchedule.hour).all()
    rows = []
    for task_id, full in (("pipeline_full", True), ("pipeline_fetch", False)):
        hours = [e.hour for e in schedule if e.run_full_pipeline == full]
        nexts = sorted(n for n in (next_by_id.get(f"pipeline_schedule_{h}") for h in hours) if n)
        rows.append(_row(task_id, ", ".join(_hour_label(h) for h in hours) or "Not scheduled",
                         nexts[0] if nexts else None, runs, paused, requested, scheduled=bool(hours)))
    from news_fetcher.breaking_feed import POLL_MINUTES
    from news_fetcher.image_focus import RUN_MINUTES
    for task_id, every in (("breaking_feeds", f"{POLL_MINUTES} min"), ("image_focus", f"{RUN_MINUTES} min"),
                           ("nyt_wire", "1 min"), ("newsbox_schedules", "1 min")):
        rows.append(_row(task_id, every, next_by_id.get(task_id), runs, paused, requested, scheduled=True))
    return rows


def _row(task_id, every, next_at, runs, paused, requested, scheduled):
    name, desc = scheduler_tasks.TASKS[task_id]
    run = runs.get(task_id) or {}
    running = bool(run.get("running_since") and (not run.get("finished_at") or run["running_since"] > run["finished_at"]))
    if task_id in paused:
        state, label = "off", "Paused"
    elif running:
        state, label = "run", "Running"
    elif run.get("status") == "error":
        state, label = "bad", "Failed"
    elif run.get("status") == "skipped":
        state, label = "warn", "Skipped"
    elif run.get("status") == "ok":
        state, label = "ok", "OK"
    else:
        state, label = "off", "Not run yet"
    return {
        "id": task_id,
        "name": name,
        "desc": desc,
        "every": every,
        "last": _when(run.get("finished_at")),
        "took": _span(run.get("seconds")),
        "summary": run.get("error") if run.get("status") == "error" else _summary_text(run.get("summary")),
        "next": "Paused" if task_id in paused else (_when(next_at) if next_at else ("—" if scheduled else "Not scheduled")),
        "state": state,
        "label": label,
        "running": running,
        "paused": task_id in paused,
        "requested": task_id in requested,
    }


def _history():
    history = _load_json_setting(SCRAPE_OUTCOME_HISTORY_KEY) or []
    rows = []
    for entry in reversed(history[-HISTORY_ROWS:]):
        timings = [t for t in entry.get("timings") or [] if t.get("seconds")]
        total = sum(t["seconds"] for t in timings) or None
        segments = []
        for key, label, _ in STAGES:
            seconds = sum(t["seconds"] for t in timings if _stage_of(t["step"]) == key)
            if seconds and total:
                segments.append({"key": key, "label": label, "pct": round(100 * seconds / total, 1),
                                 "time": _span(seconds)})
        slowest = max(timings, key=lambda t: t["seconds"]) if timings else None
        full = entry.get("run_full_pipeline")
        rows.append({
            "started": _when(entry.get("started_at")) or ("Finished " + (_when(entry.get("recorded_at")) or "?")),
            "kind": "Full" if full else ("Fetch only" if full is False else "—"),
            "took": _span(entry.get("duration_seconds")),
            "segments": segments,
            "slowest": f"Slowest: {slowest['step']} ({_span(slowest['seconds'])})" if slowest else None,
            "stored": entry.get("stored_articles"),
            "status": entry.get("status") or "ok",
        })
    return rows


def _queue():
    now = datetime.utcnow()
    live = (Job.query.filter(Job.status.in_(("running", "queued")))
            .order_by(Job.status.desc(), Job.priority.desc(), Job.id).limit(25).all())
    failed = (Job.query.filter(Job.status == "error", Job.finished_at >= now - timedelta(days=1))
              .order_by(Job.finished_at.desc()).limit(10).all())
    rows = []
    for job in live + failed:
        if job.status == "running":
            when = f"running {_span((now - job.started_at).total_seconds())}" if job.started_at else "running"
        elif job.status == "queued":
            when = f"waiting {_span((now - job.created_at).total_seconds())}"
        else:
            when = f"failed {_when(job.finished_at)}"
        rows.append({
            "id": job.id,
            "label": JOB_LABELS.get(job.kind, job.kind.replace("_", " ").capitalize()),
            "kind": job.kind,
            "status": job.status,
            "when": when,
            "error": (job.error or "")[:200] or None,
        })
    counts = jobs.queue_counts()
    return rows, counts


def _context():
    state = scheduler_tasks.state()
    runs = scheduler_tasks.runs()
    paused = scheduler_tasks.paused_ids()
    requested = scheduler_tasks.pending_run_requests()
    queue_rows, queue_counts = _queue()
    return {
        "scheduler": {
            "alive": state["alive"],
            "heartbeat": _span(state.get("heartbeat_age")) if state.get("heartbeat_age") is not None else None,
            "since": _when(state.get("started_at")),
            "worker_alive": state.get("worker_alive"),
        },
        "running": _running_now(),
        "other_running": [op for op in _running_operations() if op["kind"] == "other"],
        "recurring": _recurring(state, runs, paused, requested),
        "history": _history(),
        "stages": STAGES,
        "queue": queue_rows,
        "queue_counts": queue_counts,
    }


@admin.route("/tasks")
@role_required(ROLE_ADMIN)
def tasks_page():
    context = _context()
    if request.args.get("partial"):
        return render_template("_tasks_live.html", **context)
    return render_template("tasks.html", jobs=OVERVIEW_JOBS, **context)


@admin.route("/tasks/<task_id>/run", methods=["POST"])
@role_required(ROLE_ADMIN)
def run_task_now(task_id):
    if task_id in scheduler_tasks.TASKS:
        scheduler_tasks.request_run(task_id, getattr(current_user, "username", None))
    return redirect(url_for("admin.tasks_page"))


@admin.route("/tasks/<task_id>/pause", methods=["POST"])
@role_required(ROLE_ADMIN)
def pause_task(task_id):
    if task_id in scheduler_tasks.TASKS:
        scheduler_tasks.set_paused(task_id, request.form.get("paused") != "0")
    return redirect(url_for("admin.tasks_page"))


@admin.route("/tasks/jobs/<int:job_id>/cancel", methods=["POST"])
@role_required(ROLE_ADMIN)
def cancel_job(job_id):
    jobs.cancel(job_id)
    return redirect(url_for("admin.tasks_page"))
