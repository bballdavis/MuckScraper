"""Swipe: the deck of stories still missing a summary or analysis, and the
stories the user passed on (aggregator/skips.py). A skipped story is never
summarized by a scheduled run for that user; it can still be asked for from
the story page. /stories/{id} carries `is_skipped`.
"""
from datetime import datetime, timedelta

from flask import g, jsonify
from sqlalchemy import case, literal, or_

from aggregator import db, newsbox, skips, source_priority
from aggregator.models import Article, Edition, EditionStory, NewsBoxItem, Story
from aggregator.outlet_reporting import HIGH, LEVELS, VERY_HIGH, reporting_for

from . import api, serializers as ser
from .auth import require_token
from .errors import ApiError
from .params import MAX_PAGE_SIZE, cursor, integer, query_args
from .routes import DEFAULT_PAGE_SIZE, _annotate_read, _page, _published_editions, _story_load

# How far back the deck looks once the latest edition's headlines are done.
DECK_WINDOW = timedelta(days=7)
# The most stories the second tier considers, most articles first.
DECK_MORE_LIMIT = 500


def _blank(column):
    return or_(column.is_(None), db.func.trim(column) == "")


def _needs_work(stories):
    """The stories still missing their summary or analysis. (A one-article
    story has no analysis to write, newsbox._needs.)"""
    return [s for s in stories if not newsbox.has_parts(s, "summary_analysis")]


def _outlets(story):
    """The story's distinct outlet names: what its card counts as sources."""
    return {a.outlet.name if a.outlet else a.source for a in story.site_articles}


def _deck_order(story, tier, priority=False):
    """Sort key: priority outlets' stories first (aggregator/source_priority.py),
    then most sources (Philip, 2026-10-26), then the best reporting grade among
    them (aggregator/outlet_reporting.py), then how many outlets grade High or
    better, then the edition's headlines."""
    grades = [reporting_for(name) for name in _outlets(story)]
    best = min((LEVELS.index(g) for g in grades if g), default=len(LEVELS))
    strong = sum(g in (VERY_HIGH, HIGH) for g in grades)
    return (not priority, -len(grades), best, -strong, tier != "headlines")


def _deck(user_id):
    """[(story, tier)] in deck order: priority outlets' stories first
    (aggregator/source_priority.py), then stories with the most outlets,
    better-graded outlets breaking ties. Within a tie the latest edition's
    headlines come first by rank, then other recent stories, newest first."""
    excluded = {sid for (sid,) in db.session.query(NewsBoxItem.story_id).filter_by(user_id=user_id)}
    excluded |= skips.skipped_ids(user_id)
    missing = or_(_blank(Story.summary), _blank(Story.deep_report))
    out = []

    edition = _published_editions().order_by(Edition.created_at.desc()).first()
    if edition is not None:
        rows = (Story.query.join(EditionStory, EditionStory.story_id == Story.id)
                .filter(EditionStory.edition_id == edition.id, missing)
                .options(*_story_load()).order_by(EditionStory.rank).all())
        for story in _needs_work(rows):
            if story.id not in excluded:
                out.append((story, "headlines"))
                excluded.add(story.id)

    # Priority outlets' stories first, so the limit can't cut them; then most
    # articles, so a busy week can't push multi-source stories past the limit
    # behind a pile of new one-article ones.
    now = datetime.utcnow()
    windowed = source_priority.boosted_story_ids(since=now - DECK_WINDOW)
    priority_first = case((Story.id.in_(list(windowed)), 0), else_=1) if windowed else literal(1)
    newest = db.func.max(Article.date)
    rows = (Story.query.join(Article, Article.story_id == Story.id)
            .filter(missing).group_by(Story.id)
            .having(newest >= now - DECK_WINDOW)
            .options(*_story_load())
            .order_by(priority_first, db.func.count(Article.id).desc(), newest.desc(), Story.id.desc())
            .limit(DECK_MORE_LIMIT).all())
    rows.sort(key=lambda s: (max((a.date for a in s.articles if a.date), default=datetime.min), s.id),
              reverse=True)
    for story in _needs_work(rows):
        if story.id not in excluded:
            out.append((story, "more"))
            excluded.add(story.id)
    # A stable sort keeps rank and recency order within each tie.
    priority = source_priority.boosted_story_ids([story.id for story, _ in out])
    out.sort(key=lambda entry: _deck_order(entry[0], entry[1], entry[0].id in priority))
    return out


@api.route("/me/deck")
@require_token()
def get_deck():
    """The Swipe deck: stories without a summary or analysis that the user
    hasn't queued or skipped, from the latest edition's headlines (tier
    "headlines") and the past week's other stories (tier "more"). Stories
    from priority outlets come first, then stories with the most outlets,
    better-graded outlets breaking ties."""
    args = query_args(cursor=cursor, limit=integer(1, MAX_PAGE_SIZE))
    offset, limit = args["cursor"] or 0, args["limit"] or DEFAULT_PAGE_SIZE
    cards = []
    for story, tier in _deck(g.user.id)[offset:offset + limit + 1]:
        card = ser.story_summary(story)
        card["tier"] = tier
        cards.append(card)
    return jsonify(_page(_annotate_read(cards), offset, limit))


@api.route("/me/skips")
@require_token()
def get_skips():
    """The story ids the user skipped, most recent first."""
    args = query_args(cursor=cursor, limit=integer(1, MAX_PAGE_SIZE))
    offset, limit = args["cursor"] or 0, args["limit"] or DEFAULT_PAGE_SIZE
    rows = skips.recent(g.user.id, offset=offset, limit=limit + 1)
    return jsonify(_page([ser.story_skip(r) for r in rows], offset, limit))


@api.route("/me/skips/<int:story_id>", methods=["PUT"])
@require_token()
def skip_story(story_id):
    """Pass on a story: scheduled runs won't summarize it for this user."""
    query_args()
    row = skips.skip(g.user.id, story_id)
    if row is None:
        raise ApiError(404, "not_found", "Not found.")
    db.session.commit()
    return jsonify(ser.story_skip(row))


@api.route("/me/skips/<int:story_id>", methods=["DELETE"])
@require_token()
def unskip_story(story_id):
    """Undo a skip (204 whether or not it was skipped)."""
    query_args()
    skips.unskip(g.user.id, story_id)
    db.session.commit()
    return "", 204
