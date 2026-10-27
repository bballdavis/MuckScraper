"""NewsBox and scheduled summaries for the MuckScroller app, per account.

A story lands in a user's NewsBox when they swipe it right or save it in the
app (POST /api/v1/stories/{id}/summarize), or when their schedule runs. If
the story already has what they asked for (a summary, and the multi-source
analysis for depth "summary_analysis") the item is ready at once; otherwise
it is pending and a "newsbox_summary" job (aggregator/jobs.py) writes it.
Summaries live on the Story, so one job serves every user waiting on it.

Schedules (SummarySchedule) are JSON checked by clean_schedule(). The
scheduler container calls run_due_schedules() every minute.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func

from aggregator import jobs, skips
from aggregator.models import (Article, Edition, EditionStory, NewsBoxItem, Story, SummarySchedule, Topic, db)
from aggregator.sections import in_section

logger = logging.getLogger(__name__)

JOB_KIND = "newsbox_summary"
# Below the pipeline and the admin AI tasks (priority 10), above bulk actions.
JOB_PRIORITY = 5

# How an item got into NewsBox: a client asked for it, a bookmark saved it,
# the user's schedule picked it, or it came from the Breaking News bar.
ORIGINS = ("requested", "saved", "scheduled", "breaking")
# What an unfinished item is waiting on, in order. A Breaking headline sent
# from the app starts at "scrape" (aggregator/breaking.py); a story starts
# at "summary" or "analysis".
STEPS = ("scrape", "group", "summary", "analysis")
DEPTHS = ("summary", "analysis", "summary_analysis")
# What each depth asks to have written.
_PARTS = {"summary": {"summary"}, "analysis": {"analysis"}, "summary_analysis": {"summary", "analysis"}}
STATES = ("pending", "in_progress", "ready", "failed")
UNFINISHED = ("pending", "in_progress")

HEADLINES = "Headlines"
# Every section's `amount` is a number of stories: the top N of the latest
# edition for Headlines, the N most-covered in the last day for a topic.
MAX_SECTION_STORIES = 100
MAX_RUN_TIMES = 6
MAX_SECTIONS = 30
# The most stories one scheduled run adds, whatever the sections ask for.
MAX_STORIES_PER_RUN = 120
# "Top" stories in a section are the most-covered ones from this window.
SECTION_WINDOW = timedelta(hours=24)
DEFAULT_TIMEZONE = "America/New_York"

DEFAULT_SCHEDULE = {
    "is_enabled": False,
    "timezone": DEFAULT_TIMEZONE,
    "run_times": [],
    "sections": [],
    "depth": "summary",
    "skip_already_summarized": True,
    "minimum_sources": None,
    "notify_when_done": False,
}

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# Queueing ---------------------------------------------------------------

def can_analyze(story):
    """Whether the story has the two or more sources a multi-source analysis
    compares (Story.can_analyze). Clients hide their analysis buttons when it
    hasn't (StorySummary.can_analyze)."""
    return story.can_analyze


def _needs(story, depth):
    """The parts `depth` asks for on this story. Analysis needs two or more
    sources; with fewer, the summary stands in for it."""
    parts = set(_PARTS.get(depth, _PARTS["summary"]))
    if "analysis" in parts and not can_analyze(story):
        parts = (parts - {"analysis"}) | {"summary"}
    return parts


def _has(story, depth):
    """Whether the story already has everything `depth` asks for."""
    parts = _needs(story, depth)
    if "summary" in parts and not (story.summary or "").strip():
        return False
    if "analysis" in parts and not (story.deep_report or "").strip():
        return False
    return True


def has_parts(story, depth):
    """Public name for _has, for aggregator/breaking.py."""
    return _has(story, depth)


def _next_step(story, depth):
    """The step a pending item for `story` is waiting on."""
    parts = _needs(story, depth)
    return "summary" if "summary" in parts and not (story.summary or "").strip() else "analysis"


def _depth_of(parts):
    return next(d for d in DEPTHS if _PARTS[d] == parts)


def _deeper(a, b):
    return _depth_of(_PARTS.get(a, _PARTS["summary"]) | _PARTS.get(b, _PARTS["summary"]))


def _covers(have, want):
    return _PARTS.get(want, _PARTS["summary"]) <= _PARTS.get(have, _PARTS["summary"])


def queue_story(user_id, story, origin, depth="summary", run_id=None):
    """Put `story` in the user's NewsBox and make sure its summary gets
    written. Returns (item, queued): queued is True when a job was added.
    Caller commits (enqueue commits too, when it runs)."""
    item = NewsBoxItem.query.filter_by(user_id=user_id, story_id=story.id).first()
    if item is None:
        prev_state = prev_depth = None
        # Unread once there is something to read (the ready branches below).
        item = NewsBoxItem(user_id=user_id, story_id=story.id, origin=origin, depth=depth, run_id=run_id,
                           is_unread=False, added_at=datetime.utcnow())
        db.session.add(item)
    else:
        prev_state, prev_depth = item.state, item.depth
        item.depth = _deeper(item.depth, depth)
    db.session.flush()
    if _has(story, item.depth):
        if item.state != "ready":
            item.state, item.ready_at, item.error, item.is_unread = "ready", datetime.utcnow(), None, True
        item.step = None
        db.session.flush()
        return item, False
    covered = ((prev_state in UNFINISHED and _covers(prev_depth, item.depth))
               or _job_in_flight(story.id, item.depth, exclude_id=item.id))
    item.state, item.error, item.step = "pending", None, _next_step(story, item.depth)
    db.session.flush()
    if covered:
        return item, False
    jobs.enqueue(JOB_KIND, {"story_id": story.id, "depth": item.depth}, priority=JOB_PRIORITY)
    return item, True


def queue_summary(story, depth="summary"):
    """Make sure `story` gets what `depth` asks for, with nobody's NewsBox
    waiting on it (the web Breaking News bar). Returns True when a job was
    queued. Commits when it queues."""
    if _has(story, depth) or _job_in_flight(story.id, depth):
        return False
    jobs.enqueue(JOB_KIND, {"story_id": story.id, "depth": depth}, priority=JOB_PRIORITY)
    return True


def queue_feed_entry(user_id, entry, depth="summary"):
    """Put a Breaking headline in the user's NewsBox. Once it is in a story
    this is queue_story(); before that the item waits on the scrape job
    (aggregator/breaking.py), which attaches the story. Caller commits."""
    story = entry.article.story if entry.article is not None else None
    if story is not None:
        return queue_story(user_id, story, "breaking", depth)[0]
    item = NewsBoxItem.query.filter_by(user_id=user_id, feed_entry_id=entry.id, story_id=None).first()
    if item is None:
        item = NewsBoxItem(user_id=user_id, feed_entry_id=entry.id, origin="breaking", depth=depth,
                           is_unread=False, added_at=datetime.utcnow())
        db.session.add(item)
    else:
        item.depth = _deeper(item.depth, depth)
    item.state, item.step, item.error = "pending", "scrape", None
    db.session.flush()
    return item


def attach_story(item, story):
    """The scrape job found the item's story: give it the story and queue its
    summary. When the user already has that story in NewsBox, the two merge
    into the older item. Caller commits."""
    existing = NewsBoxItem.query.filter(NewsBoxItem.user_id == item.user_id, NewsBoxItem.story_id == story.id,
                                        NewsBoxItem.id != item.id).first()
    depth = item.depth
    if existing is not None:
        db.session.delete(item)
        db.session.flush()
        return queue_story(existing.user_id, story, existing.origin, depth)[0]
    item.story_id = story.id
    if _has(story, depth):
        item.state, item.ready_at, item.error, item.is_unread, item.step = "ready", datetime.utcnow(), None, True, None
        db.session.flush()
        return item
    item.state, item.error, item.step = "pending", None, _next_step(story, depth)
    db.session.flush()
    # Not queue_story(): this item was waiting on the scrape, not a summary.
    if not _job_in_flight(story.id, depth, exclude_id=item.id):
        jobs.enqueue(JOB_KIND, {"story_id": story.id, "depth": depth}, priority=JOB_PRIORITY)
    return item


def _set_step(story_id, step):
    for item in _waiting(story_id):
        item.step = step
    db.session.commit()


def _job_in_flight(story_id, depth, exclude_id=None):
    """A queued or running job already covers this story at this depth: some
    other unfinished item for it asks for at least as much. One job per
    story, however many users are waiting on it."""
    wanted = [d for d in DEPTHS if _covers(d, depth)]
    query = db.session.query(NewsBoxItem.id).filter(
        NewsBoxItem.story_id == story_id,
        NewsBoxItem.state.in_(UNFINISHED),
        NewsBoxItem.depth.in_(wanted),
    )
    if exclude_id is not None:
        query = query.filter(NewsBoxItem.id != exclude_id)
    return query.first() is not None


def _waiting(story_id):
    return NewsBoxItem.query.filter(NewsBoxItem.story_id == story_id, NewsBoxItem.state.in_(UNFINISHED)).all()


def run_summary_job(payload):
    """Job handler: write what the waiting items need, then mark them ready."""
    story = Story.query.get(payload["story_id"])
    if story is None:
        raise RuntimeError(f"Story {payload['story_id']} no longer exists.")
    for item in _waiting(story.id):
        item.state = "in_progress"
    db.session.commit()

    depth = payload.get("depth") if payload.get("depth") in DEPTHS else "summary"
    if not _has(story, depth):
        from news_fetcher.llm_client import TIER_QUALITY
        from news_fetcher.summarizer import check_ollama_status, generate_deep_report, summarize_story
        if not check_ollama_status(TIER_QUALITY):
            raise RuntimeError("The summarization model is offline.")
        parts = _needs(story, depth)
        if "summary" in parts and not (story.summary or "").strip():
            _set_step(story.id, "summary")
            summary = summarize_story(story)
            if not summary:
                raise RuntimeError("No summary was written.")
            story.summary = summary
            db.session.commit()
        if not _has(story, depth):
            _set_step(story.id, "analysis")
            report = generate_deep_report(story)
            if not report:
                raise RuntimeError("No analysis was written.")
            story.deep_report = report
            db.session.commit()

    now = datetime.utcnow()
    for item in _waiting(story.id):
        if _has(story, item.depth):
            item.state, item.ready_at, item.error, item.is_unread, item.step = "ready", now, None, True, None
        else:
            # Asked for more while this job ran; queue_story added a job for it.
            item.state, item.step = "pending", _next_step(story, item.depth)
    db.session.commit()


def fail_summary_job(payload, message):
    for item in _waiting(payload.get("story_id")):
        item.state, item.error, item.step = "failed", (message or "Failed.")[:300], None
    db.session.commit()


jobs.register(JOB_KIND, run_summary_job, fail_summary_job)


# Schedules --------------------------------------------------------------

class ScheduleError(ValueError):
    """A schedule the app sent doesn't make sense; the message says why."""


def _flag(raw, name, default):
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ScheduleError(f"{name} must be true or false.")
    return value


def _whole(value, low, high, name):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ScheduleError(f"{name} must be a whole number from {low} to {high}.")
    return value


def clean_schedule(raw):
    """A schedule dict from the app (or the database), checked field by field.
    Raises ScheduleError naming the first problem. Missing fields take the
    defaults; unknown ones are rejected."""
    if not isinstance(raw, dict):
        raise ScheduleError("Expected a JSON object.")
    unknown = set(raw) - set(DEFAULT_SCHEDULE)
    if unknown:
        raise ScheduleError(f"Unknown field: {sorted(unknown)[0][:40]}.")
    out = {
        "is_enabled": _flag(raw, "is_enabled", DEFAULT_SCHEDULE["is_enabled"]),
        "skip_already_summarized": _flag(raw, "skip_already_summarized", True),
        "notify_when_done": _flag(raw, "notify_when_done", False),
    }

    tz = raw.get("timezone", DEFAULT_TIMEZONE)
    try:
        if not isinstance(tz, str) or len(tz) > 64:
            raise ZoneInfoNotFoundError(tz)
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError("timezone must be an IANA time zone such as America/New_York.")
    out["timezone"] = tz

    depth = raw.get("depth", "summary")
    if depth not in DEPTHS:
        raise ScheduleError(f"depth must be one of: {', '.join(DEPTHS)}.")
    out["depth"] = depth

    minimum = raw.get("minimum_sources")
    out["minimum_sources"] = None if minimum is None else _whole(minimum, 1, 50, "minimum_sources")

    run_times = raw.get("run_times", [])
    if not isinstance(run_times, list) or len(run_times) > MAX_RUN_TIMES:
        raise ScheduleError(f"run_times must be a list of up to {MAX_RUN_TIMES}.")
    out["run_times"], ids = [], set()
    for run in run_times:
        if not isinstance(run, dict) or set(run) - {"id", "time", "days", "is_enabled"}:
            raise ScheduleError("Each run time has id, time, days and is_enabled.")
        run_id = run.get("id")
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 40 or run_id in ids:
            raise ScheduleError("Each run time needs its own id of 1 to 40 characters.")
        ids.add(run_id)
        if not isinstance(run.get("time"), str) or not _TIME.match(run["time"]):
            raise ScheduleError("time must be HH:MM, 24-hour.")
        days = run.get("days")
        if (not isinstance(days, list) or not days or len(set(days)) != len(days)
                or any(not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 7 for d in days)):
            raise ScheduleError("days must list ISO weekdays, 1 (Monday) to 7, each once.")
        out["run_times"].append({"id": run_id, "time": run["time"], "days": sorted(days),
                                 "is_enabled": _flag(run, "is_enabled", True)})

    sections = raw.get("sections", [])
    if not isinstance(sections, list) or len(sections) > MAX_SECTIONS:
        raise ScheduleError(f"sections must be a list of up to {MAX_SECTIONS}.")
    out["sections"], names = [], set()
    for rule in sections:
        if not isinstance(rule, dict) or set(rule) - {"section", "is_enabled", "amount"}:
            raise ScheduleError("Each section has section, is_enabled and amount.")
        name = rule.get("section")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or name in names:
            raise ScheduleError("Each section needs its own name of 1 to 80 characters.")
        names.add(name)
        out["sections"].append({"section": name, "is_enabled": _flag(rule, "is_enabled", True),
                                "amount": _whole(rule.get("amount", 1), 1, MAX_SECTION_STORIES, f"{name} amount")})
    return out


def load_schedule(user_id):
    """(settings dict, SummarySchedule or None)."""
    row = SummarySchedule.query.get(user_id)
    if row is None:
        return dict(DEFAULT_SCHEDULE), None
    try:
        return clean_schedule(json.loads(row.settings or "{}")), row
    except (ValueError, ScheduleError):
        return dict(DEFAULT_SCHEDULE), row


def next_run(settings, after):
    """The first enabled run time after `after` (naive UTC), as naive UTC, or None."""
    if not settings["is_enabled"]:
        return None
    runs = [r for r in settings["run_times"] if r["is_enabled"]]
    if not runs:
        return None
    tz = ZoneInfo(settings["timezone"])
    local = after.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    best = None
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        for run in runs:
            if day.isoweekday() not in run["days"]:
                continue
            hour, minute = map(int, run["time"].split(":"))
            at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            if at > local and (best is None or at < best):
                best = at
        if best is not None:
            break
    return best.astimezone(ZoneInfo("UTC")).replace(tzinfo=None) if best else None


def save_schedule(user_id, raw):
    """Check and store the user's schedule; returns (settings, row). Caller commits."""
    settings = clean_schedule(raw)
    row = SummarySchedule.query.get(user_id)
    if row is None:
        row = SummarySchedule(user_id=user_id)
        db.session.add(row)
    row.settings = json.dumps(settings)
    row.next_run_at = next_run(settings, datetime.utcnow())
    row.updated_at = datetime.utcnow()
    return settings, row


def _source_count(story):
    return len({a.outlet.name if a.outlet else a.source for a in story.site_articles})


def _candidates(rule, now):
    """Stories a section rule picks, best first, more than it needs so the
    filters can drop some."""
    if rule["section"] == HEADLINES:
        edition = (Edition.query.filter_by(published=True).order_by(Edition.created_at.desc()).first())
        if edition is None:
            return [], 0
        rows = (EditionStory.query.filter_by(edition_id=edition.id).order_by(EditionStory.rank)
                .limit(rule["amount"] * 2).all())
        return [r.story for r in rows if r.story is not None], rule["amount"]
    topic = Topic.query.filter(func.lower(Topic.name) == rule["section"].lower()).first()
    if topic is None:
        return [], 0
    stories = (Story.query.join(Article).filter(in_section(topic), Article.date >= now - SECTION_WINDOW)
               .group_by(Story.id).order_by(func.count(Article.id).desc(), func.max(Article.date).desc())
               .limit(max(rule["amount"] * 4, 40)).all())
    return stories, rule["amount"]


def pick_stories(user_id, settings, now=None):
    """The stories a run of `settings` adds to the user's NewsBox."""
    now = now or datetime.utcnow()
    have = {sid for (sid,) in db.session.query(NewsBoxItem.story_id).filter_by(user_id=user_id)}
    # Stories the user passed on in Swipe are only ever run by hand.
    have |= skips.skipped_ids(user_id)
    picked, seen = [], set()
    for rule in settings["sections"]:
        if not rule["is_enabled"]:
            continue
        candidates, wanted = _candidates(rule, now)
        taken = 0
        for story in candidates:
            if taken >= wanted or len(picked) >= MAX_STORIES_PER_RUN:
                break
            if story.id in have or story.id in seen:
                continue
            if settings["skip_already_summarized"] and _has(story, settings["depth"]):
                continue
            if settings["minimum_sources"] and _source_count(story) < settings["minimum_sources"]:
                continue
            picked.append(story)
            seen.add(story.id)
            taken += 1
    return picked


def run_schedule(user_id, settings, now=None):
    """Add one run's stories to the user's NewsBox. Returns (run_id, count)."""
    now = now or datetime.utcnow()
    run_id = f"run-{user_id}-{now:%Y%m%d%H%M%S}"
    stories = pick_stories(user_id, settings, now)
    for story in stories:
        queue_story(user_id, story, "scheduled", settings["depth"], run_id=run_id)
    db.session.commit()
    return run_id, len(stories)


def run_due_schedules(now=None):
    """Run every enabled schedule whose time has come, then set its next run.
    Called by the scheduler every minute; SKIP LOCKED keeps two callers from
    running the same schedule twice."""
    now = now or datetime.utcnow()
    rows = (SummarySchedule.query.filter(SummarySchedule.next_run_at <= now)
            .with_for_update(skip_locked=True).all())
    due = []
    for row in rows:
        settings, _ = load_schedule(row.user_id)
        # Move each schedule on before running it, so a run that fails is
        # not retried every minute.
        row.last_run_at = now
        row.next_run_at = next_run(settings, now)
        if settings["is_enabled"] and row.user is not None and row.user.is_active:
            due.append((row.user_id, settings))
    db.session.commit()
    ran = 0
    for user_id, settings in due:
        try:
            run_schedule(user_id, settings, now)
            ran += 1
        except Exception:
            db.session.rollback()
            logger.exception("[NewsBox] Scheduled run for user %s failed", user_id)
    return ran
