"""Admin › Pipeline: what the scheduler is doing (Run now, Pause, the job
queue, recent runs), the one-off jobs, the hours the pipeline runs, and a
manual fetch. The same data and rules as blueprints/admin/tasks_page.py,
tools.py, bulk_actions.py, config_crud.py and articles.py.

Every long job is queued for the scheduler's job worker, so no request here
runs longer than the API container allows.
"""
import logging
import os

from flask import g, jsonify

from aggregator import db, jobs, scheduler_tasks
from aggregator.models import ROLE_SCRAPER, PipelineSchedule, Topic

from ..errors import ApiError
from . import can_admin, reply, route
from . import values as v
from .values import Invalid

logger = logging.getLogger(__name__)

# The one-off jobs, keyed as the app names them. Each is a bulk task the job
# worker runs, but Wake Ollama, which only sends a packet.
ONE_OFF = ("wake_ollama", "ollama_catchup", "scrape_all_missing", "sync_allsides", "reclassify_articles",
           "audit_scrapes", "force_resummarize", "merge_outlets")
_WEB_NAMES = {"wake_ollama": "Wake Ollama", "sync_allsides": "Sync AllSides ratings",
              "merge_outlets": "Merge duplicate outlets"}


def _tasks_context():
    from aggregator.blueprints.admin.tasks_page import _context
    return _context()


def _one_off_jobs():
    """[(key, OVERVIEW_JOBS entry)] in ONE_OFF order."""
    from aggregator.blueprints.admin.tools import OVERVIEW_JOBS
    by_key = {}
    for job in OVERVIEW_JOBS:
        key = job.get("bulk") or next((k for k, name in _WEB_NAMES.items() if name == job["name"]), None)
        if key:
            by_key[key] = job
    return [(key, by_key[key]) for key in ONE_OFF if key in by_key]


def _job_status(key):
    from aggregator.blueprints.admin._tasks import _bulk_task_status_payload
    if key == "wake_ollama":
        return None
    payload = _bulk_task_status_payload(key)
    status = payload.get("status")
    return {
        "state": status if status in ("running", "success", "error") else "idle",
        "message": payload.get("message") if status in ("running", "error") else None,
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
    }


def _one_off():
    out = []
    for key, job in _one_off_jobs():
        out.append({
            "key": key,
            "name": job["name"],
            "description": job["desc"],
            "risk": job["risk"],
            "confirm": job.get("confirm"),
            "available": key != "wake_ollama" or bool(os.environ.get("OLLAMA_MAC")),
            "status": _job_status(key),
        })
    return out


def _tasks():
    c = _tasks_context()
    running = c["running"]
    return {
        "scheduler": c["scheduler"],
        "running": running,
        "other_running": [{"label": op["label"], "started_at": op.get("started_at")} for op in c["other_running"]],
        "recurring": [{**r, "pausable": r["id"] not in scheduler_tasks.UNPAUSABLE} for r in c["recurring"]],
        "queue": c["queue"],
        "queue_counts": c["queue_counts"],
        "history": c["history"],
        "stages": [{"key": key, "label": label} for key, label, _ in c["stages"]],
        "jobs": _one_off(),
    }


@route("/admin/tasks")
def tasks():
    """The scheduler, the run in progress, recurring tasks, the job queue, recent runs and the one-off jobs."""
    return jsonify(_tasks())


def _task(task_id):
    if task_id not in scheduler_tasks.TASKS:
        raise ApiError(404, "not_found", "No such task.")
    return task_id


@route("/admin/tasks/<task_id>/run", methods=("POST",))
def run_task(task_id):
    """Ask the scheduler to run a recurring task now."""
    scheduler_tasks.request_run(_task(task_id), g.user.username)
    return reply(_tasks(), "Asked the scheduler to run it. It starts within a few seconds.")


@route("/admin/tasks/<task_id>/paused", methods=("PUT",))
def pause_task(task_id):
    """{paused}"""
    _task(task_id)
    if task_id in scheduler_tasks.UNPAUSABLE:
        raise Invalid("This task can't be paused.")
    paused = v.get_bool(v.body(), "paused")
    scheduler_tasks.set_paused(task_id, paused)
    return reply(_tasks(), "Paused." if paused else "Resumed.")


@route("/admin/jobs/<int:job_id>/cancel", methods=("POST",))
def cancel_job(job_id):
    """Cancel a job that's still waiting."""
    if not jobs.cancel(job_id):
        raise Invalid("That job already started or finished.")
    return reply(_tasks(), "Job cancelled.")


def _wake_ollama():
    mac = os.environ.get("OLLAMA_MAC", "")
    if not mac:
        raise Invalid("OLLAMA_MAC isn't set, so there's no machine to wake.")
    try:
        import wakeonlan
        wakeonlan.send_magic_packet(mac)
    except Exception as e:
        logger.error("WoL error: %s", e)
        raise Invalid("The wake packet couldn't be sent.")
    return "Sent the wake packet. Ollama is usually up within a minute."


@route("/admin/actions/<key>", methods=("POST",))
def run_action(key):
    """Start a one-off job. Long ones are queued for the scheduler's job worker."""
    from aggregator.blueprints.admin._tasks import _start_bulk_task
    jobs_by_key = dict(_one_off_jobs())
    if key not in jobs_by_key:
        raise ApiError(404, "not_found", "No such job.")
    if key == "wake_ollama":
        return reply(_tasks(), _wake_ollama())
    started, _status = _start_bulk_task(key)
    name = jobs_by_key[key]["name"]
    return reply(_tasks(), f"{name}: queued. The job worker starts it shortly." if started
                 else f"{name} is already running.")


# --- Run schedule ----------------------------------------------------------------------

def _hour_label(h):
    return f"{(h % 12) or 12}:00 {'AM' if h < 12 else 'PM'}"


def _changed():
    from aggregator.blueprints.admin.config_crud import _mark_pipeline_schedule_changed
    _mark_pipeline_schedule_changed()


def _schedule():
    from aggregator.blueprints.admin.config_crud import pipeline_schedule_restart_needed
    entries = PipelineSchedule.query.order_by(PipelineSchedule.hour.asc()).all()
    return {
        "items": [{"id": e.id, "hour": e.hour, "label": _hour_label(e.hour), "full": bool(e.run_full_pipeline),
                   "active": bool(e.is_active)} for e in entries],
        "hours": [{"value": h, "label": _hour_label(h)} for h in range(24)],
        "timezone": "America/New_York",
        "not_applied": bool(pipeline_schedule_restart_needed()),
    }


def _hour(values, exclude_id=None):
    hour = v.get_int(values, "hour", 0, 23, label="Hour")
    clash = PipelineSchedule.query.filter(PipelineSchedule.hour == hour)
    if exclude_id is not None:
        clash = clash.filter(PipelineSchedule.id != exclude_id)
    if clash.first():
        raise Invalid(f"There's already a run at {_hour_label(hour)}.", "hour")
    return hour


def _entry(entry_id):
    entry = db.session.get(PipelineSchedule, entry_id)
    if entry is None:
        raise ApiError(404, "not_found", "No such run.")
    return entry


@route("/admin/schedule")
def schedule():
    """The hours the pipeline runs, Eastern time."""
    return jsonify(_schedule())


@route("/admin/schedule", methods=("POST",))
def add_run():
    """{hour, full}"""
    values = v.body()
    hour = _hour(values)
    full = v.get_bool(values, "full") if "full" in values else True
    db.session.add(PipelineSchedule(hour=hour, run_full_pipeline=full, is_active=True))
    db.session.commit()
    _changed()
    return reply(_schedule(), f"Added a run at {_hour_label(hour)}.")


@route("/admin/schedule/<int:entry_id>", methods=("PATCH",))
def update_run(entry_id):
    """Any of {hour, full, active}."""
    e = _entry(entry_id)
    values = v.body()
    if "hour" in values:
        e.hour = _hour(values, exclude_id=e.id)
    if "full" in values:
        e.run_full_pipeline = v.get_bool(values, "full")
    if "active" in values:
        e.is_active = v.get_bool(values, "active")
    db.session.commit()
    _changed()
    return reply(_schedule())


@route("/admin/schedule/<int:entry_id>", methods=("DELETE",))
def delete_run(entry_id):
    """Stop running at that hour."""
    e = _entry(entry_id)
    label = _hour_label(e.hour)
    db.session.delete(e)
    db.session.commit()
    _changed()
    return reply(_schedule(), f"No longer running at {label}.")


# --- Fetch -----------------------------------------------------------------------------
# Open to scrapers as on the web (Fetch Articles is a scraper page), so these
# don't need the admin scope.

NEWSAPI_CATEGORIES = ("business", "science", "sports", "general", "health", "technology")
GNEWS_CATEGORIES = ("business", "science", "sports", "world", "nation", "general")


def _require_scraper():
    if not (can_admin() or g.user.has_role(ROLE_SCRAPER)):
        raise ApiError(403, "forbidden", "Fetching articles needs a scraper or admin account.")


def _fetch():
    from aggregator.blueprints.admin import MANUAL_FETCH_STATUS_KEY
    from aggregator.blueprints.admin._shared import _load_json_setting, fetch_presets
    status = _load_json_setting(MANUAL_FETCH_STATUS_KEY) or {}
    return {
        "presets": [{"key": str(i), **p} for i, p in enumerate(fetch_presets())],
        "sections": [{"id": t.id, "name": t.display_name or t.label}
                     for t in Topic.query.filter_by(is_active=True).order_by(Topic.sort_order)],
        "newsapi_categories": list(NEWSAPI_CATEGORIES),
        "gnews_categories": list(GNEWS_CATEGORIES),
        "status": {
            "running": status.get("status") == "running",
            "label": status.get("label"),
            "started_at": status.get("started_at"),
            "finished_at": status.get("finished_at"),
            "error": status.get("error"),
        },
    }


@route("/admin/fetch", admin=False)
def fetch():
    """The fetch presets (the scheduler's own fetches), the categories, and the last manual fetch."""
    _require_scraper()
    return jsonify(_fetch())


@route("/admin/fetch", methods=("POST",), admin=False)
def start_fetch():
    """{preset} for a preset, or {mode: top|query, label, country, category, gnews_category, query, gnews_query}."""
    from aggregator.blueprints.admin._shared import fetch_presets
    from aggregator.blueprints.admin._tasks import start_manual_fetch
    _require_scraper()
    values = v.body()
    if values.get("preset") is not None:
        presets = fetch_presets()
        index = v.get_int(values, "preset", 0, max(len(presets) - 1, 0), label="Preset")
        if not presets:
            raise Invalid("There are no presets.", "preset")
        p = presets[index]
        params = {key: p.get(key) or None for key in ("label", "mode", "country", "category", "sources", "query",
                                                     "gnews_query", "gnews_category")}
    else:
        mode = v.get_choice(values, "mode", ("top", "query"), "Pick top headlines or a search.")
        params = {"mode": mode, "label": v.get_str(values, "label", 80, required=True, label="Label")}
        if mode == "top":
            params["country"] = v.get_str(values, "country", 8) or "us"
            params["category"] = v.get_choice(values, "category", NEWSAPI_CATEGORIES)
            params["gnews_category"] = v.get_choice(values, "gnews_category", GNEWS_CATEGORIES)
        else:
            params["query"] = v.get_str(values, "query", 300, required=True, label="Query")
            params["gnews_query"] = v.get_str(values, "gnews_query", 300) or None
    if not start_manual_fetch(params):
        raise Invalid("A manual fetch is already running. Wait for it to finish.")
    return reply(_fetch(), f"Fetching {params['label'] or 'Custom'}. New articles show up as they're stored.")
