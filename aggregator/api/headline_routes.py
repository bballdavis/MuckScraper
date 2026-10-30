"""Headlines' Trending / Latest / Controversial views for the app
(aggregator/headline_views.py), read live like the web page. Stories the
user marked read are left out and counted in `hidden_read`, as the web page
does. ?topic= gives a section's own three views (Philip, 2026-10-27).
"""
from datetime import datetime

from flask import g, jsonify

from aggregator import headline_views, reads
from aggregator.models import Topic

from . import api, serializers as ser
from .errors import ApiError
from .auth import require_token
from .params import MAX_PAGE_SIZE, cursor, enum, integer, query_args, text as text_param
from .routes import DEFAULT_PAGE_SIZE, _page


def _card(story, view, now):
    entry = story.rank_entry
    newest = min(entry.signals.newest, now) if entry.signals.newest else None
    card = ser.story_summary(story)
    card.update({
        "is_read": False,
        "why": headline_views.short_why(view, entry),
        "newest_at": ser.iso(newest),
        "group": headline_views.hour_group(newest, now) if view == "latest" and newest else None,
        "framing_gap": headline_views.gap_level(entry.gap),
        "opeds": entry.opeds[0] if entry.opeds else 0,
        "arrivals": ser.arrivals(story, now) if view == "trending" else None,
    })
    return card


@api.route("/headlines")
@require_token()
def headlines():
    """One view's stories in display order, plus every view's count for the
    switcher. Omitting view gives Trending, the default; topic narrows all
    three to that section."""
    args = query_args(view=enum(*headline_views.VIEWS), topic=text_param(80), cursor=cursor,
                      limit=integer(1, MAX_PAGE_SIZE))
    view = args["view"] or headline_views.DEFAULT_VIEW
    offset, limit = args["cursor"] or 0, args["limit"] or DEFAULT_PAGE_SIZE
    read_ids = reads.read_ids(g.user.id, "story")

    topic_id = None
    if args["topic"]:
        topic = Topic.query.filter_by(name=args["topic"]).first()
        if topic is None:
            raise ApiError(404, "not_found", "No such topic.")
        topic_id = topic.id

    now = datetime.utcnow()
    built = headline_views.build(now=now, topic_id=topic_id, user_id=g.user.id)
    shown = {key: [e for e in entries if e.story_id not in read_ids] for key, entries in built.items()}
    stories = headline_views.load_stories(shown[view][offset:offset + limit + 1])
    return jsonify({
        "view": view,
        "topic": args["topic"],
        "views": [{"key": key, "label": headline_views.LABELS[key][0], "description": headline_views.LABELS[key][1],
                   "count": len(shown[key])} for key in headline_views.VIEWS],
        "hidden_read": len(built[view]) - len(shown[view]),
        **_page([_card(s, view, now) for s in stories], offset, limit),
    })
