"""The app's per-account writes: send a story to NewsBox, read and clear
NewsBox, and edit or run the user's scheduled summaries (aggregator/newsbox.py).

Nothing here runs a model in the request. A summary is queued as a job for
the scheduler container's worker; the response says whether the story is
ready or pending. Queueing needs the "summarize" scope and counts against
the per-user daily limits in Settings › API.
"""
from datetime import datetime

from flask import g, jsonify, request
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from aggregator import db, newsbox, reads
from aggregator.blocklist import IngestionBlocks
from aggregator.models import NewsBoxItem, Story
from news_fetcher.breaking_feed import is_junk

from . import api, serializers as ser
from .auth import require_token
from .errors import ApiError
from .params import MAX_PAGE_SIZE, bad, boolean, cursor, enum, integer, json_body, query_args
from .ratelimit import check_schedule_runs, check_summaries
from .routes import DEFAULT_PAGE_SIZE, _page, _story_load


def _item_query():
    return NewsBoxItem.query.filter_by(user_id=g.user.id).options(
        joinedload(NewsBoxItem.story).options(*_story_load()), joinedload(NewsBoxItem.feed_entry))


def _junk_headline(item, blocks):
    """A Breaking headline with no story yet that the poller would now refuse
    (ingestion blocks, gambling, promos; breaking_feed.is_junk). Saved before
    that filter existed, so NewsBox hides it; the row stays until removed."""
    return item.story_id is None and item.feed_entry is not None and is_junk(
        item.feed_entry.title, item.feed_entry.url, blocks)


def _not_read(query, user_id):
    """Leaves out the stories the user has marked read. Marking one read takes
    it out of NewsBox (Recent keeps it, and Unread or Undo brings it back), so
    the list doesn't depend on each phone's copy of the read list, which only
    holds its first page after a refresh. A Breaking headline has no story to
    read and stays."""
    read = reads.read_ids(user_id, "story")
    if not read:
        return query
    return query.filter(or_(NewsBoxItem.story_id.is_(None), NewsBoxItem.story_id.notin_(read)))


def _flag(value):
    if not isinstance(value, bool):
        raise bad("Must be true or false.")
    return value


def _own_item(story_id):
    item = _item_query().filter_by(story_id=story_id).first()
    if item is None:
        raise ApiError(404, "not_found", "That story isn't in your NewsBox.")
    return item


@api.route("/stories/<int:story_id>/summarize", methods=["POST"])
@require_token("summarize")
def summarize_story(story_id):
    """Send a story to the user's NewsBox: 200 when it is already summarized
    (ready to read), 202 when a summary has been queued (pending). With
    refresh, a summary or analysis that sources have arrived since is
    written again (the app's Refresh buttons)."""
    body = json_body(depth=enum(*newsbox.DEPTHS), origin=enum("requested", "saved"), refresh=_flag)
    story = Story.query.options(*_story_load()).get(story_id)
    if story is None:
        raise ApiError(404, "not_found", "Not found.")
    if NewsBoxItem.query.filter_by(user_id=g.user.id, story_id=story_id).first() is None:
        check_summaries(g.user)
    item, _ = newsbox.queue_story(g.user.id, story, body["origin"] or "requested", body["depth"] or "summary",
                                 refresh=bool(body["refresh"]))
    db.session.commit()
    return jsonify(ser.newsbox_item(item)), 200 if item.state == "ready" else 202


@api.route("/me/newsbox")
@require_token()
def get_newsbox():
    """The user's NewsBox, newest first. unread_count covers the whole NewsBox."""
    args = query_args(state=enum(*newsbox.STATES), origin=enum(*newsbox.ORIGINS), unread=boolean,
                      cursor=cursor, limit=integer(1, MAX_PAGE_SIZE))
    offset, limit = args["cursor"] or 0, args["limit"] or DEFAULT_PAGE_SIZE
    query = _not_read(_item_query(), g.user.id)
    for name in ("state", "origin"):
        if args[name]:
            query = query.filter(getattr(NewsBoxItem, name) == args[name])
    if args["unread"] is not None:
        query = query.filter(NewsBoxItem.is_unread.is_(args["unread"]))
    rows = query.order_by(NewsBoxItem.added_at.desc(), NewsBoxItem.id.desc()).offset(offset).limit(limit + 1).all()
    blocks = IngestionBlocks()
    page = _page(rows, offset, limit)
    page["items"] = [ser.newsbox_item(i) for i in page["items"] if not _junk_headline(i, blocks)]
    unread = _not_read(NewsBoxItem.query.filter_by(user_id=g.user.id, is_unread=True), g.user.id).count()
    unread_headlines = (NewsBoxItem.query.filter_by(user_id=g.user.id, is_unread=True, story_id=None)
                        .options(joinedload(NewsBoxItem.feed_entry)).all())
    unread -= sum(1 for i in unread_headlines if _junk_headline(i, blocks))
    return jsonify({**page, "unread_count": unread})


@api.route("/me/newsbox/<int:story_id>", methods=["PATCH"])
@require_token()
def update_newsbox_item(story_id):
    body = json_body(is_unread=_flag)
    item = _own_item(story_id)
    if body["is_unread"] is not None:
        item.is_unread = body["is_unread"]
    db.session.commit()
    return jsonify(ser.newsbox_item(item))


@api.route("/me/newsbox/<int:story_id>", methods=["DELETE"])
@require_token()
def remove_newsbox_item(story_id):
    item = _own_item(story_id)
    db.session.delete(item)
    db.session.commit()
    return "", 204


@api.route("/me/newsbox/items/<int:item_id>", methods=["DELETE"])
@require_token()
def remove_newsbox_entry(item_id):
    """Take out one NewsBox item by its id. A Breaking headline with no story
    yet has no story id to name, so the app removes it this way."""
    item = _item_query().filter_by(id=item_id).first()
    if item is None:
        raise ApiError(404, "not_found", "That item isn't in your NewsBox.")
    db.session.delete(item)
    db.session.commit()
    return "", 204


@api.route("/me/newsbox/read", methods=["POST"])
@require_token()
def mark_newsbox_read():
    """Mark every item read."""
    updated = (NewsBoxItem.query.filter_by(user_id=g.user.id, is_unread=True)
               .update({"is_unread": False}, synchronize_session=False))
    db.session.commit()
    return jsonify({"updated": updated})


@api.route("/me/schedules")
@require_token()
def get_schedule():
    settings, row = newsbox.load_schedule(g.user.id)
    return jsonify(ser.schedule(settings, row))


@api.route("/me/schedules", methods=["PUT"])
@require_token("summarize")
def put_schedule():
    """Replace the user's schedule. Fields left out take their defaults."""
    raw = request.get_json(silent=True)
    if isinstance(raw, dict):
        # The app sends back what it read; these two are the server's to set.
        raw = {k: v for k, v in raw.items() if k not in ("next_run_at", "last_run_at")}
    try:
        settings, row = newsbox.save_schedule(g.user.id, raw)
    except newsbox.ScheduleError as e:
        raise bad(str(e))
    db.session.commit()
    return jsonify(ser.schedule(settings, row))


@api.route("/me/schedules/run", methods=["POST"])
@require_token("summarize")
def run_schedule_now():
    """Run the user's schedule once, now, whether or not it is switched on."""
    check_schedule_runs(g.user)
    settings, _ = newsbox.load_schedule(g.user.id)
    if not any(rule["is_enabled"] for rule in settings["sections"]):
        raise bad("Turn on at least one section first.")
    run_id, count = newsbox.run_schedule(g.user.id, settings, datetime.utcnow())
    return jsonify({"run_id": run_id, "added": count}), 202
