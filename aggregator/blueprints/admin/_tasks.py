import json
import logging
import importlib
from datetime import datetime
from flask import request, jsonify
from aggregator.permissions import ROLE_ADMIN, ROLE_SCRAPER, role_required
from sqlalchemy import or_
from aggregator import db, jobs
from aggregator.models import AppSetting, Article, Story
from news_fetcher.llm_client import TIER_QUALITY

from . import admin, _TASK_STATUS_KEY_PREFIXES, ORPHANED_TASK_STALE_AFTER, BULK_TASK_ACTIONS
from ._shared import _load_json_setting, _save_json_setting

logger = logging.getLogger(__name__)


def _try_claim_task_status(key, running_payload):
    """
    Atomically claim a task-status row for 'running', safe across multiple
    gunicorn worker processes (not just threads within one process).

    A plain "check status, then save 'running'" -- even guarded by an
    in-process threading.Lock -- only prevents a double-start from threads in
    the SAME process. With multiple worker processes, two requests landing on
    different workers would each pass the check independently (each process
    has its own lock and Python globals) and both start the same action.

    This does the check-and-set as one atomic SQL statement instead: the
    UPDATE (or INSERT, if the row doesn't exist yet) only takes effect if no
    other process already has the row marked 'running', and the database's
    row-level locking makes that atomic regardless of how many processes are
    asking at once. Returns True if this call won the claim, False if the
    action is already running (started by any process/thread).
    """
    payload_json = json.dumps(running_payload)
    result = db.session.execute(
        db.text("""
            INSERT INTO app_settings (key, value)
            VALUES (:key, :value)
            ON CONFLICT (key) DO UPDATE
                SET value = :value
                WHERE (app_settings.value::json ->> 'status') IS DISTINCT FROM 'running'
            RETURNING key
        """),
        {"key": key, "value": payload_json},
    )
    claimed = result.fetchone() is not None
    db.session.commit()
    return claimed


def reconcile_orphaned_task_statuses():
    """Mark any task-status stuck 'running' well past a plausible runtime as
    interrupted, so a task whose process died mid-run doesn't permanently
    block that action from being re-run (_start_bulk_task/etc. all refuse to
    start an action whose status is already 'running').

    Deliberately keyed on staleness (started_at age), not "found running at
    this process's boot": with multiple gunicorn worker processes (see
    GUNICORN_WORKERS), a 'running' status can be perfectly legitimate work
    happening in a *different* worker than the one currently starting up, so
    "I just (re)started, therefore every running status is dead" is not a
    safe assumption once there's more than one worker. Age-based staleness
    is correct regardless of how many workers or processes are involved, so
    this can run at any worker's startup (or be called periodically) without
    risk of killing a task that's actually still alive elsewhere.
    """
    cutoff = datetime.utcnow() - ORPHANED_TASK_STALE_AFTER
    rows = AppSetting.query.filter(
        or_(*[AppSetting.key.like(prefix + "%") for prefix in _TASK_STATUS_KEY_PREFIXES])
    ).all()
    reconciled = 0
    for row in rows:
        try:
            payload = json.loads(row.value) if row.value else None
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("status") != "running":
            continue

        started_at = payload.get("started_at")
        try:
            started_dt = datetime.fromisoformat(started_at) if started_at else None
        except (TypeError, ValueError):
            started_dt = None
        if started_dt is None or started_dt > cutoff:
            # No parseable start time, or still within a plausible runtime --
            # leave it alone; it may genuinely still be running, possibly in
            # a different worker process than this one.
            continue

        payload["status"] = "error"
        payload["finished_at"] = datetime.utcnow().isoformat()
        payload["message"] = (
            f"No update in over {int(ORPHANED_TASK_STALE_AFTER.total_seconds() // 3600)}h; "
            "treated as interrupted (process likely died mid-task). Re-run to retry."
        )
        row.value = json.dumps(payload)
        reconciled += 1
    if reconciled:
        db.session.commit()
        logger.info("[Startup] Reconciled %d stale 'running' task status(es).", reconciled)
    return reconciled


def _ai_task_status_key(task_type, resource_id):
    return f"ai_task_status_v1:{task_type}:{resource_id}"


def _ai_task_default_message(task_type):
    messages = {
        "story_summary": "Story summary has not been started in this app session yet.",
        "story_deep_report": "Story analysis has not been started in this app session yet.",
        "article_summary": "Article summary has not been started in this app session yet.",
    }
    return messages.get(task_type, "AI task has not been started in this app session yet.")


def _ai_task_status_payload(task_type, resource_id):
    payload = _load_json_setting(_ai_task_status_key(task_type, resource_id))
    if payload:
        return payload
    return {
        "status": "idle",
        "task_type": task_type,
        "resource_id": resource_id,
        "started_at": None,
        "finished_at": None,
        "message": _ai_task_default_message(task_type),
    }


def _save_ai_task_status(task_type, resource_id, payload):
    base_payload = {
        "task_type": task_type,
        "resource_id": resource_id,
    }
    base_payload.update(payload)
    _save_json_setting(_ai_task_status_key(task_type, resource_id), base_payload)


def _run_ai_task(payload):
    """Job handler (aggregator/jobs.py) for one story/article AI task."""
    task_type = payload["task_type"]
    resource_id = payload["resource_id"]
    started_at = datetime.utcnow().isoformat()
    _save_ai_task_status(task_type, resource_id, {
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "message": "AI task is running.",
    })

    from news_fetcher.summarizer import (
        summarize_story,
        summarize_article,
        generate_deep_report,
        check_ollama_status,
    )

    if not check_ollama_status(TIER_QUALITY):
        raise RuntimeError("The summarization LLM is offline.")

    if task_type == "story_summary":
        story = Story.query.get(resource_id)
        if story is None:
            raise RuntimeError(f"Story {resource_id} no longer exists.")
        summary = summarize_story(story)
        if not summary:
            raise RuntimeError("No story summary was generated.")
        story.summary = summary
        db.session.commit()
        message = "Story summary completed successfully."
    elif task_type == "story_deep_report":
        story = Story.query.get(resource_id)
        if story is None:
            raise RuntimeError(f"Story {resource_id} no longer exists.")
        if not story.can_analyze:
            raise RuntimeError("Story analysis requires at least two sources.")
        report = generate_deep_report(story)
        if not report:
            raise RuntimeError("No story analysis was generated.")
        story.deep_report = report
        db.session.commit()
        message = "Story analysis completed successfully."
    elif task_type == "article_summary":
        article = Article.query.get(resource_id)
        if article is None:
            raise RuntimeError(f"Article {resource_id} no longer exists.")
        summary = summarize_article(article)
        if not summary:
            raise RuntimeError("No article summary was generated.")
        article.summary = summary
        db.session.commit()
        message = "Article summary completed successfully."
    else:
        raise RuntimeError(f"Unknown AI task type: {task_type}")

    _save_ai_task_status(task_type, resource_id, {
        "status": "success",
        "started_at": started_at,
        "finished_at": datetime.utcnow().isoformat(),
        "message": message,
    })


def _fail_ai_task(payload, message):
    task_type = payload["task_type"]
    resource_id = payload["resource_id"]
    started_at = _ai_task_status_payload(task_type, resource_id).get("started_at")
    _save_ai_task_status(task_type, resource_id, {
        "status": "error",
        "started_at": started_at,
        "finished_at": datetime.utcnow().isoformat(),
        "message": message,
    })


def _bulk_task_status_key(action):
    return f"bulk_task_status_v1:{action}"


def _bulk_task_status_payload(action):
    payload = _load_json_setting(_bulk_task_status_key(action))
    if payload:
        return payload
    return {
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "message": f"{action} has not been run in this app session yet.",
    }


# What each bulk action runs, as "module:function", so a queued job names it
# by action and the worker imports it.
BULK_TASK_FUNCTIONS = {
    "ollama_catchup": "news_fetcher.fetch_and_store_articles:ollama_catchup",
    "force_resummarize": "news_fetcher.fetch_and_store_articles:force_resummarize_all",
    "reclassify_articles": "news_fetcher.fetch_and_store_articles:reclassify_all_articles",
    "audit_scrapes": "news_fetcher.fetch_and_store_articles:audit_existing_scrapes",
    "scrape_all_missing": "aggregator.blueprints.admin.articles:_scrape_all_missing_task",
}


def _run_bulk_task(payload):
    """Job handler (aggregator/jobs.py) for one admin bulk action."""
    action = payload["action"]
    module_name, function_name = BULK_TASK_FUNCTIONS[action].split(":")
    fn = getattr(importlib.import_module(module_name), function_name)
    started_at = datetime.utcnow().isoformat()
    _save_json_setting(_bulk_task_status_key(action), {
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "message": f"{action} is running.",
    })
    fn()
    _save_json_setting(_bulk_task_status_key(action), {
        "status": "success",
        "started_at": started_at,
        "finished_at": datetime.utcnow().isoformat(),
        "message": f"{action} completed successfully.",
    })
    logger.info(f"[BulkTask] {action} completed")


def _fail_bulk_task(payload, message):
    action = payload["action"]
    _save_json_setting(_bulk_task_status_key(action), {
        "status": "error",
        "started_at": _bulk_task_status_payload(action).get("started_at"),
        "finished_at": datetime.utcnow().isoformat(),
        "message": message,
    })


jobs.register("bulk_task", _run_bulk_task, _fail_bulk_task)
jobs.register("ai_task", _run_ai_task, _fail_ai_task)

# Clicked on one story or article and watched, so ahead of bulk actions.
AI_TASK_PRIORITY = 10
BULK_TASK_PRIORITY = 0
QUEUED_MESSAGE = "Queued; the scheduler container's worker will start it shortly."


def _start_bulk_task(action):
    """
    Queue a long-running admin action (reclassify, resummarize, regroup,
    scrape backlog, etc.) for the job worker in the scheduler container.
    These can take minutes on a full DB, which was blowing past gunicorn's
    worker timeout (GitHub issue #3), and a thread inside a gunicorn worker
    died with it on any restart.

    Returns (started, status) — started is False (with the current status)
    if this action is already queued or running.
    """
    if action not in BULK_TASK_FUNCTIONS:
        raise ValueError(f"Unknown bulk action {action!r}")
    claimed = _try_claim_task_status(
        _bulk_task_status_key(action),
        {
            "status": "running",
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
            "message": QUEUED_MESSAGE,
        },
    )
    if not claimed:
        return False, _bulk_task_status_payload(action)

    jobs.enqueue("bulk_task", {"action": action}, priority=BULK_TASK_PRIORITY)
    return True, _bulk_task_status_payload(action)


@admin.route("/bulk-task-status/<action>")
@role_required(ROLE_ADMIN)
def bulk_task_status(action):
    if action not in BULK_TASK_ACTIONS:
        return jsonify({"status": "error", "message": "Unknown action."}), 404
    return jsonify(_bulk_task_status_payload(action))


@admin.route("/ai-task/start", methods=["POST"])
@role_required(ROLE_SCRAPER)
def start_ai_task():
    payload = request.get_json(silent=True) or request.form
    task_type = (payload.get("task_type") or "").strip()
    resource_id = payload.get("resource_id")

    try:
        resource_id = int(resource_id)
    except (TypeError, ValueError):
        return jsonify({
            "started": False,
            "message": "Invalid resource id.",
        }), 400

    if task_type not in {"story_summary", "story_deep_report", "article_summary"}:
        return jsonify({
            "started": False,
            "message": "Invalid AI task type.",
        }), 400

    claimed = _try_claim_task_status(
        _ai_task_status_key(task_type, resource_id),
        {
            "task_type": task_type,
            "resource_id": resource_id,
            "status": "running",
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
            "message": QUEUED_MESSAGE,
        },
    )
    if not claimed:
        return jsonify({
            "started": False,
            "status": _ai_task_status_payload(task_type, resource_id),
        }), 409

    jobs.enqueue(
        "ai_task",
        {"task_type": task_type, "resource_id": resource_id},
        priority=AI_TASK_PRIORITY,
    )

    return jsonify({
        "started": True,
        "status": _ai_task_status_payload(task_type, resource_id),
    }), 202


@admin.route("/ai-task-status/<task_type>/<int:resource_id>")
@role_required(ROLE_SCRAPER)
def ai_task_status(task_type, resource_id):
    if task_type not in {"story_summary", "story_deep_report", "article_summary"}:
        return jsonify({
            "status": "error",
            "message": "Invalid AI task type.",
        }), 400
    return jsonify(_ai_task_status_payload(task_type, resource_id))

