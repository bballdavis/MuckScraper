import json
import logging
from datetime import datetime
import requests
from flask import render_template, request, redirect, url_for
from aggregator.permissions import ROLE_ADMIN, role_required
from aggregator.models import AppSetting

from . import admin, DOCKER_RESTART_PROXY_URL, CONTAINER_SERVICE_NAMES, MANUAL_FETCH_STATUS_KEY, FETCH_RUN_STATUS_KEY, BULK_TASK_ACTIONS, ORPHANED_TASK_STALE_AFTER
from ._shared import _load_json_setting, _save_json_setting
from ._tasks import _bulk_task_status_payload
from .config_crud import pipeline_schedule_restart_needed

logger = logging.getLogger(__name__)


# The Overview's job list, safest first (settings mock, 2026-10-22). `bulk` is
# the BULK_TASK_ACTIONS key whose status the page polls; `post` is the form
# endpoint (or None for a script-run job); `confirm` asks before broad and
# dangerous ones.
OVERVIEW_JOBS = [
    {"name": "Wake Ollama", "desc": "Send the Wake-on-LAN packet to the Ollama machine",
     "risk": "safe", "post": "admin.wake_ollama", "button": "Wake"},
    {"name": "Run AI catch-up", "desc": "Fill missing summaries and ratings without a full pipeline run",
     "risk": "safe", "post": "admin.ollama_catchup_route", "button": "Run", "bulk": "ollama_catchup"},
    {"name": "Scrape missing", "desc": "Retry recent articles with no content, or marked failed or pending",
     "risk": "safe", "post": "admin.scrape_all_missing", "button": "Run", "bulk": "scrape_all_missing"},
    {"name": "Sync AllSides ratings", "desc": "Refresh outlet bias from the AllSides mapping and pass it on to articles",
     "risk": "maintenance", "post": "admin.sync_allsides", "button": "Run"},
    {"name": "Reclassify topics", "desc": "Re-run topic classification on stored stories and articles",
     "risk": "broad", "post": "admin.reclassify_articles", "button": "Run", "bulk": "reclassify_articles",
     "confirm": "Reclassify every stored story and article? Topics can change across the site."},
    {"name": "Audit stored scrapes", "desc": "Catch login walls, duplicate junk and weak content in stored articles",
     "risk": "broad", "post": "admin.audit_scrapes", "button": "Run", "bulk": "audit_scrapes",
     "confirm": "Re-audit every stored scrape? Articles that fail are marked for repair."},
    {"name": "Rebuild story summaries", "desc": "Clear and regenerate every summary and deep report",
     "risk": "danger", "post": "admin.force_resummarize", "button": "Run", "bulk": "force_resummarize",
     "confirm": "Clear and regenerate every story summary and deep report? This takes a long time and can't be undone."},
    {"name": "Merge duplicate outlets", "desc": "Normalize outlet names and merge duplicates onto one outlet",
     "risk": "danger", "post": None, "button": "Run", "script": "merge-outlets",
     "confirm": "Merge and rename duplicate outlets? This changes stored outlet records and article associations."},
]


def _next_scheduled_run():
    """The next active PipelineSchedule firing, as (datetime, is_full) in the
    schedule's own zone (America/New_York), or None when nothing is scheduled."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from aggregator.models import PipelineSchedule

    entries = PipelineSchedule.query.filter_by(is_active=True).all()
    if not entries:
        return None
    now = datetime.now(ZoneInfo("America/New_York"))
    best = None
    for entry in entries:
        at = now.replace(hour=entry.hour, minute=0, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        if best is None or at < best[0]:
            best = (at, entry.run_full_pipeline)
    return best


def _job_queue_counts():
    from aggregator.models import Job
    try:
        return {
            "queued": Job.query.filter_by(status="queued").count(),
            "running": Job.query.filter_by(status="running").count(),
        }
    except Exception as e:  # the jobs table is newer than some installs' schema
        logger.warning("[Overview] Could not count jobs: %s", e)
        return None


@admin.route("/tools")
@role_required(ROLE_ADMIN)
def tools_page():
    running_ops = _running_operations()
    fetch_ops = [op for op in running_ops if op["kind"] == "fetch"]
    other_ops = [op for op in running_ops if op["kind"] == "other"]
    containers = _list_containers()

    return render_template(
        "admin_tools.html",
        containers=_container_rows(containers),
        containers_running=sum(1 for c in containers if c.get("status") == "running"),
        pipeline_schedule_restart_needed=pipeline_schedule_restart_needed(),
        fetch_running_ops=fetch_ops,
        other_running_ops=other_ops,
        next_run=_next_scheduled_run(),
        job_queue=_job_queue_counts(),
        jobs=OVERVIEW_JOBS,
        restart_blocked_reason=request.args.get("blocked_reason"),
        restart_blocked_target=request.args.get("blocked_target"),
    )


@admin.route("/toggle-auto-article-deep-analysis", methods=["POST"])
@role_required(ROLE_ADMIN)
def toggle_auto_article_deep_analysis():
    from news_fetcher.fetch_and_store_articles import (
        AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY,
    )
    enabled = request.form.get("enabled") == "true"
    _save_json_setting(AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY, enabled)
    logger.info(f"[Settings] auto_article_deep_analysis_enabled set to {enabled}")
    return redirect(url_for("admin.llm_providers_page"))


@admin.route("/containers/<name>/restart", methods=["POST"])
@role_required(ROLE_ADMIN)
def restart_container(name):
    force = request.form.get("force") == "true"
    blocking = _blocking_operations_for(name)
    if blocking and not force:
        reason = f"Can't restart '{name}' right now — still running: {_describe_blocking(blocking)}"
        logger.warning("[ContainerRestart] Refused restart of %s: %s", name, reason)
        return redirect(url_for("admin.tools_page", blocked_reason=reason, blocked_target=name))
    if blocking and force:
        logger.warning(
            "[ContainerRestart] Forcing restart of %s despite running: %s",
            name, _describe_blocking(blocking),
        )

    try:
        response = requests.post(f"{DOCKER_RESTART_PROXY_URL}/containers/{name}/restart", timeout=35)
        response.raise_for_status()
        logger.info("[ContainerRestart] Restarted %s", name)
    except requests.RequestException as e:
        logger.error("[ContainerRestart] Failed to restart %s: %s", name, e)
        return redirect(url_for("admin.tools_page", blocked_reason=f"Failed to restart '{name}': {e}"))

    return redirect(url_for("admin.tools_page"))


@admin.route("/containers/restart-all", methods=["POST"])
@role_required(ROLE_ADMIN)
def restart_all_containers():
    force = request.form.get("force") == "true"
    blocking = _running_operations()
    if blocking and not force:
        reason = f"Can't restart all — still running: {_describe_blocking(blocking)}"
        logger.warning("[ContainerRestart] Refused restart-all: %s", reason)
        return redirect(url_for("admin.tools_page", blocked_reason=reason, blocked_target="all"))
    if blocking and force:
        logger.warning(
            "[ContainerRestart] Forcing restart-all despite running: %s",
            _describe_blocking(blocking),
        )

    try:
        response = requests.post(f"{DOCKER_RESTART_PROXY_URL}/restart-all", timeout=120)
        response.raise_for_status()
        logger.info("[ContainerRestart] Restart-all results: %s", response.json())
    except requests.RequestException as e:
        # Expected once the app container itself gets restarted mid-call --
        # the response never makes it back. Log and move on either way.
        logger.info("[ContainerRestart] Restart-all request ended (%s) -- expected if 'app' was restarted.", e)

    return redirect(url_for("admin.tools_page"))


def _is_running_and_fresh(payload):
    """True if a task-status payload is 'running' and still within a
    plausible runtime -- reuses ORPHANED_TASK_STALE_AFTER so a process that
    died mid-task (no restart involved at all) doesn't block restarts
    forever; the next real run overwrites the flag anyway."""
    if not payload or payload.get("status") != "running":
        return False
    started_at = payload.get("started_at")
    try:
        started_dt = datetime.fromisoformat(started_at) if started_at else None
    except (TypeError, ValueError):
        started_dt = None
    if started_dt is None:
        return True
    return started_dt > datetime.utcnow() - ORPHANED_TASK_STALE_AFTER


def _running_operations():
    """
    Every background/long-running operation currently active that a
    container restart could interrupt, each tagged with which of this
    project's containers restarting would interrupt (`affects`). Covers:
    scheduled fetch/pipeline runs, manual on-demand fetches, the six bulk
    admin actions and per-resource AI tasks.
    """
    ops = []

    fetch_status = _load_json_setting(FETCH_RUN_STATUS_KEY)
    if _is_running_and_fresh(fetch_status):
        kind = "full pipeline" if fetch_status.get("run_full_pipeline") else "fetch-only"
        ops.append({
            "kind": "fetch",
            "label": f"Scheduled {kind} run",
            "affects": {"scheduler", "postgres"},
            "started_at": fetch_status.get("started_at"),
        })

    manual_status = _load_json_setting(MANUAL_FETCH_STATUS_KEY)
    if _is_running_and_fresh(manual_status):
        ops.append({
            "kind": "fetch",
            "label": f"Manual fetch ({manual_status.get('label') or 'Custom'})",
            "affects": {"app", "postgres"},
            "started_at": manual_status.get("started_at"),
        })

    for action in BULK_TASK_ACTIONS:
        payload = _bulk_task_status_payload(action)
        if _is_running_and_fresh(payload):
            ops.append({
                "kind": "other",
                "label": action.replace("_", " ").title(),
                # Runs in the scheduler container's job worker (aggregator/jobs.py).
                "affects": {"scheduler", "postgres"},
                "started_at": payload.get("started_at"),
            })

    ai_rows = AppSetting.query.filter(AppSetting.key.like("ai_task_status_v1:%")).all()
    for row in ai_rows:
        try:
            payload = json.loads(row.value) if row.value else None
        except (TypeError, ValueError):
            continue
        if _is_running_and_fresh(payload):
            ops.append({
                "kind": "other",
                "label": f"AI task: {payload.get('task_type')} #{payload.get('resource_id')}",
                "affects": {"scheduler", "postgres"},
                "started_at": payload.get("started_at"),
            })

    return ops


def _blocking_operations_for(container_name):
    return [op for op in _running_operations() if container_name in op["affects"]]


def _list_containers():
    """Container status via the docker-restart-proxy. Returns [] (and logs
    a warning) rather than raising if the proxy is unreachable, so a proxy
    hiccup doesn't take down the whole Admin Tools page."""
    try:
        response = requests.get(f"{DOCKER_RESTART_PROXY_URL}/containers", timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.warning("[ContainerRestart] Could not reach docker-restart-proxy: %s", e)
        return []


def _parse_docker_time(value):
    """Docker's RFC 3339 times carry nanoseconds; datetime takes micro."""
    if not value or value.startswith("0001-"):
        return None
    try:
        head, _, frac = value.rstrip("Z").partition(".")
        stamp = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S")
        if frac:
            stamp = stamp.replace(microsecond=int(frac[:6].ljust(6, "0")))
        return stamp
    except ValueError:
        return None


def _short_span(delta):
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def _container_rows(containers):
    """The proxy's containers in a fixed order (the known services first),
    each with a health level (ok/warn/bad) and the facts the Overview shows."""
    order = {name: i for i, name in enumerate(CONTAINER_SERVICE_NAMES)}
    now = datetime.utcnow()
    rows = []
    for c in sorted(containers, key=lambda c: (order.get(c.get("service"), len(order)), c.get("service") or "")):
        running = c.get("status") == "running"
        health = c.get("health")
        started = _parse_docker_time(c.get("started_at"))
        finished = _parse_docker_time(c.get("finished_at"))
        if not running:
            level = "bad"
            state = c.get("status") or "unknown"
            if c.get("exit_code") not in (None, 0):
                state += f" (exit {c['exit_code']})"
            since = f"{_short_span(now - finished)} ago" if finished else None
        else:
            level = {"unhealthy": "bad", "starting": "warn"}.get(health, "ok")
            state = health if health else "running"
            since = f"up {_short_span(now - started)}" if started else None
        rows.append({
            **c,
            "level": level,
            "state": state,
            "since": since,
            "image_short": (c.get("image") or "").rsplit("/", 1)[-1] or None,
        })
    return rows


def _describe_blocking(ops):
    return "; ".join(
        f"{op['label']} (started {op['started_at']})" for op in ops
    )
