"""Headlines' Trending / Latest / Controversial views for the app
(aggregator/headline_views.py), read live like the web page. Stories the
user marked read are left out and counted in `hidden_read`, as the web page
does.
"""
from datetime import datetime

from flask import g, jsonify

from aggregator import headline_views, reads

from . import api, serializers as ser
from .auth import require_token
from .params import MAX_PAGE_SIZE, cursor, enum, integer, query_args
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
    })
    return card


@api.route("/headlines")
@require_token()
def headlines():
    """One view's stories in display order, plus every view's count for the
    switcher. Omitting view gives Trending, the default."""
    args = query_args(view=enum(*headline_views.VIEWS), cursor=cursor, limit=integer(1, MAX_PAGE_SIZE))
    view = args["view"] or headline_views.DEFAULT_VIEW
    offset, limit = args["cursor"] or 0, args["limit"] or DEFAULT_PAGE_SIZE
    read_ids = reads.read_ids(g.user.id, "story")

    now = datetime.utcnow()
    built = headline_views.build(now=now)
    shown = {key: [e for e in entries if e.story_id not in read_ids] for key, entries in built.items()}
    stories = headline_views.load_stories(shown[view][offset:offset + limit + 1])
    return jsonify({
        "view": view,
        "views": [{"key": key, "label": headline_views.LABELS[key][0], "description": headline_views.LABELS[key][1],
                   "count": len(shown[key])} for key in headline_views.VIEWS],
        "hidden_read": len(built[view]) - len(shown[view]),
        **_page([_card(s, view, now) for s in stories], offset, limit),
    })
