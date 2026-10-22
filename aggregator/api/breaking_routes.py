"""Breaking News for the app (aggregator/breaking.py): the same headlines as
the bar on Headlines, and sending one to NewsBox, which scrapes it (when it
isn't in a story yet) and summarizes its story in the background. Sending
needs the "summarize" scope and counts against the per-user daily limit,
like any story sent to NewsBox.
"""
from datetime import datetime

from flask import g, jsonify

from aggregator import breaking, newsbox
from aggregator.models import FeedEntry, NewsBoxItem

from . import api, serializers as ser
from .auth import require_token
from .errors import ApiError
from .params import enum, json_body
from .ratelimit import check_summaries


@api.route("/breaking")
@require_token()
def get_breaking():
    """The newest breaking headlines, newest first (at most three)."""
    now = datetime.utcnow()
    return jsonify({"items": [ser.breaking_item(e, breaking.entry_state(e, now)) for e in breaking.bar_entries(now=now)]})


@api.route("/breaking/<int:entry_id>/newsbox", methods=["POST"])
@require_token("summarize")
def send_breaking_to_newsbox(entry_id):
    """Put a breaking headline in the user's NewsBox: 200 when its story is
    already summarized, 202 while it is scraped and summarized."""
    body = json_body(depth=enum(*newsbox.DEPTHS))
    entry = FeedEntry.query.get(entry_id)
    if entry is None:
        raise ApiError(404, "not_found", "Not found.")
    story = breaking.story_of(entry)
    already = NewsBoxItem.query.filter_by(user_id=g.user.id, feed_entry_id=entry.id).first() is not None or (
        story is not None and NewsBoxItem.query.filter_by(user_id=g.user.id, story_id=story.id).first() is not None)
    if not already:
        check_summaries(g.user)
    item = breaking.request(entry, body["depth"] or "summary", user_id=g.user.id)
    return jsonify(ser.newsbox_item(item)), 200 if item.state == "ready" else 202
