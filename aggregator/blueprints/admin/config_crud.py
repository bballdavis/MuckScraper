import logging
from datetime import datetime
from flask import jsonify, render_template, request, redirect, url_for
from aggregator.permissions import ROLE_ADMIN, role_required
from sqlalchemy import func
from aggregator import db
from aggregator.models import AppSetting, Topic, PromptTemplate, PipelineSchedule, ScheduledFetch
from news_fetcher.prompt_registry import validate_prompt_text, invalidate_cache as invalidate_prompt_cache, KNOWN_VARS as PROMPT_KNOWN_VARS

from . import admin

logger = logging.getLogger(__name__)


@admin.route("/prompts")
@role_required(ROLE_ADMIN)
def prompts_page():
    prompts = PromptTemplate.query.order_by(PromptTemplate.key.asc()).all()
    return render_template("prompts.html", prompts=prompts)


@admin.route("/prompts/<key>/edit")
@role_required(ROLE_ADMIN)
def edit_prompt(key):
    prompt = PromptTemplate.query.filter_by(key=key).first_or_404()
    saved = request.args.get("saved") == "1"
    return render_template(
        "prompt_edit.html", prompt=prompt, submitted_text=prompt.current_text,
        error=None, warning=None, saved=saved, known_vars=sorted(PROMPT_KNOWN_VARS.get(key, set())),
    )


@admin.route("/prompts/<key>/update", methods=["POST"])
@role_required(ROLE_ADMIN)
def update_prompt(key):
    prompt = PromptTemplate.query.filter_by(key=key).first_or_404()
    text = request.form.get("current_text", "")
    known_vars = sorted(PROMPT_KNOWN_VARS.get(key, set()))

    error, warning = validate_prompt_text(key, text)
    if error:
        return render_template(
            "prompt_edit.html", prompt=prompt, submitted_text=text,
            error=error, warning=None, saved=False, known_vars=known_vars,
        )

    prompt.current_text = text
    prompt.updated_at = datetime.utcnow()
    db.session.commit()
    invalidate_prompt_cache(key)

    if warning:
        return render_template(
            "prompt_edit.html", prompt=prompt, submitted_text=prompt.current_text,
            error=None, warning=warning, saved=True, known_vars=known_vars,
        )
    return redirect(url_for("admin.edit_prompt", key=key, saved=1))


@admin.route("/prompts/<key>/reset", methods=["POST"])
@role_required(ROLE_ADMIN)
def reset_prompt(key):
    prompt = PromptTemplate.query.filter_by(key=key).first_or_404()
    prompt.current_text = prompt.default_text
    prompt.updated_at = None
    db.session.commit()
    invalidate_prompt_cache(key)
    return redirect(url_for("admin.edit_prompt", key=key, saved=1))


def _mark_pipeline_schedule_changed():
    """Record that PipelineSchedule was edited, to tell whether the running
    scheduler has applied it yet (pipeline_schedule_restart_needed)."""
    now = datetime.utcnow().isoformat()
    setting = AppSetting.query.filter_by(key="pipeline_schedule_changed_at").first()
    if setting:
        setting.value = now
    else:
        db.session.add(AppSetting(key="pipeline_schedule_changed_at", value=now))
    db.session.commit()


def pipeline_schedule_restart_needed():
    """Whether a schedule edit is still waiting on the scheduler. A running
    scheduler applies edits on its next control tick (aggregator/
    scheduler_tasks.py), so this is only true while it's down or stuck, or
    on a build from before it re-read the schedule."""
    from aggregator.scheduler_tasks import schedule_applied_since
    changed = AppSetting.query.filter_by(key="pipeline_schedule_changed_at").first()
    if not changed or not changed.value:
        return False
    return not schedule_applied_since(changed.value)


@admin.route("/pipeline-schedule")
@role_required(ROLE_ADMIN)
def pipeline_schedule_page():
    entries = PipelineSchedule.query.order_by(PipelineSchedule.hour.asc()).all()
    return render_template(
        "pipeline_schedule.html", entries=entries, restart_needed=pipeline_schedule_restart_needed(),
    )


@admin.route("/pipeline-schedule/add", methods=["POST"])
@role_required(ROLE_ADMIN)
def add_pipeline_schedule():
    hour = request.form.get("hour", type=int)
    run_full_pipeline = request.form.get("run_full_pipeline") == "on"

    if hour is not None and 0 <= hour <= 23 and not PipelineSchedule.query.filter_by(hour=hour).first():
        db.session.add(PipelineSchedule(
            hour=hour,
            run_full_pipeline=run_full_pipeline,
            is_active=True,
        ))
        db.session.commit()
        _mark_pipeline_schedule_changed()
        logger.info(f"[PipelineSchedule] Added scheduled run at {hour}:00 (full_pipeline={run_full_pipeline})")
    return redirect(url_for("admin.pipeline_schedule_page"))


@admin.route("/pipeline-schedule/<int:entry_id>/update", methods=["POST"])
@role_required(ROLE_ADMIN)
def update_pipeline_schedule(entry_id):
    entry = PipelineSchedule.query.get_or_404(entry_id)
    hour = request.form.get("hour", type=int)

    if hour is not None and 0 <= hour <= 23:
        entry.hour = hour
    entry.run_full_pipeline = request.form.get("run_full_pipeline") == "on"
    entry.is_active = request.form.get("is_active") == "on"
    db.session.commit()
    _mark_pipeline_schedule_changed()
    return redirect(url_for("admin.pipeline_schedule_page", _anchor=f"run-{entry.id}"))


@admin.route("/pipeline-schedule/<int:entry_id>/delete", methods=["POST"])
@role_required(ROLE_ADMIN)
def delete_pipeline_schedule(entry_id):
    entry = PipelineSchedule.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    _mark_pipeline_schedule_changed()
    return redirect(url_for("admin.pipeline_schedule_page"))


SCHEDULED_FETCH_MODES = ["query", "top"]


def _scheduled_fetch_form_values(form):
    """
    Pull a ScheduledFetch's editable fields off a submitted form.

    The unused half of the mode pair is blanked rather than kept: leaving a
    stale `query` on a row switched to "top" mode would be invisible in the
    UI but still sitting in the DB, ready to confuse the next person who
    switches it back.
    """
    mode = (form.get("mode", "") or "").strip()
    if mode not in SCHEDULED_FETCH_MODES:
        mode = "query"

    def field(name):
        return (form.get(name, "") or "").strip() or None

    values = {
        "mode":           mode,
        "description":    field("description"),
        "gnews_query":    field("gnews_query"),
        "gnews_category": field("gnews_category"),
        "newsapi_country":  field("newsapi_country") if mode == "top" else None,
        "newsapi_category": field("newsapi_category") if mode == "top" else None,
        "newsapi_query":    field("newsapi_query") if mode == "query" else None,
        "known_outlets_only": form.get("known_outlets_only") == "on",
    }
    # The section this search serves; blank is General (the classifier decides).
    topic_id = form.get("topic_id", type=int)
    values["topic_id"] = topic_id if topic_id and db.session.get(Topic, topic_id) else None
    return values


@admin.route("/scheduled-fetches")
@role_required(ROLE_ADMIN)
def scheduled_fetches_page():
    fetches = ScheduledFetch.query.order_by(
        ScheduledFetch.sort_order.asc(), ScheduledFetch.id.asc()
    ).all()
    topics = Topic.query.order_by(Topic.sort_order.asc().nullslast(), Topic.name.asc()).all()
    return render_template(
        "scheduled_fetches.html", fetches=fetches, modes=SCHEDULED_FETCH_MODES, topics=topics,
    )


@admin.route("/scheduled-fetches/add", methods=["POST"])
@role_required(ROLE_ADMIN)
def add_scheduled_fetch():
    label = request.form.get("label", "").strip()

    if label and not ScheduledFetch.query.filter_by(label=label).first():
        values = _scheduled_fetch_form_values(request.form)
        next_order = (db.session.query(func.max(ScheduledFetch.sort_order)).scalar() or 0) + 1
        db.session.add(ScheduledFetch(
            label=label, sort_order=next_order, is_active=True, **values
        ))
        db.session.commit()
        logger.info(f"[ScheduledFetches] Added fetch: {label} (mode={values['mode']})")
    return redirect(url_for("admin.scheduled_fetches_page"))


@admin.route("/scheduled-fetches/<int:fetch_id>/update", methods=["POST"])
@role_required(ROLE_ADMIN)
def update_scheduled_fetch(fetch_id):
    fetch = ScheduledFetch.query.get_or_404(fetch_id)
    label = request.form.get("label", "").strip()
    sort_order = request.form.get("sort_order", type=int)

    # Labels key per-run metrics, so a collision would silently merge two
    # fetches' results -- reject the edit and keep the existing label.
    clash = ScheduledFetch.query.filter(
        ScheduledFetch.label == label, ScheduledFetch.id != fetch.id
    ).first()
    if label and not clash:
        fetch.label = label

    for key, value in _scheduled_fetch_form_values(request.form).items():
        setattr(fetch, key, value)
    if sort_order is not None:
        fetch.sort_order = sort_order
    # The card's switch saves on its own (set_scheduled_fetch_active), so the
    # editor leaves the state alone unless a form sends it.
    if "is_active" in request.form:
        fetch.is_active = request.form.get("is_active") in ("on", "true", "1")
    db.session.commit()
    return redirect(url_for("admin.scheduled_fetches_page", _anchor=f"fetch-{fetch.id}"))


@admin.route("/scheduled-fetches/<int:fetch_id>/active", methods=["POST"])
@role_required(ROLE_ADMIN)
def set_scheduled_fetch_active(fetch_id):
    """The card's switch: turn one fetch on or off without touching the rest."""
    fetch = ScheduledFetch.query.get_or_404(fetch_id)
    payload = request.get_json(silent=True) or {}
    fetch.is_active = bool(payload.get("active"))
    db.session.commit()
    return jsonify({"id": fetch.id, "active": fetch.is_active})


@admin.route("/scheduled-fetches/reorder", methods=["POST"])
@role_required(ROLE_ADMIN)
def reorder_scheduled_fetches():
    """Drag and drop: the page posts every fetch id in its new run order."""
    payload = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in payload.get("ids", [])]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be integers"}), 400
    fetches = {f.id: f for f in ScheduledFetch.query.all()}
    if sorted(ids) != sorted(fetches):
        return jsonify({"error": "ids must list every scheduled fetch once"}), 400
    for position, fetch_id in enumerate(ids, start=1):
        fetches[fetch_id].sort_order = position
    db.session.commit()
    return jsonify({"ids": ids})


@admin.route("/scheduled-fetches/<int:fetch_id>/delete", methods=["POST"])
@role_required(ROLE_ADMIN)
def delete_scheduled_fetch(fetch_id):
    fetch = ScheduledFetch.query.get_or_404(fetch_id)
    db.session.delete(fetch)
    db.session.commit()
    return redirect(url_for("admin.scheduled_fetches_page"))
