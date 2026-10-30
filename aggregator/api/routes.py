"""The /api/v1 endpoints (docs/API.md, served spec at /api/v1/openapi.json).

Read-only over content: nothing here fetches, scrapes, runs a model or
touches an admin setting. The only writes are a device's own pairing,
sign-out and last-used time, and the user's own feed preferences.
"""
from datetime import datetime, timedelta

from flask import g, jsonify, request
from sqlalchemy import func, text
from sqlalchemy.orm import joinedload, selectinload

from aggregator import article_jobs, article_lean, db, feed_prefs, headline_views, reads, skips, source_priority
from aggregator.models import Article, Edition, EditionStory, OpinionPiece, Story, Topic
from aggregator.opinion import published_edition_opinion_cards
from aggregator.search import SearchUnavailableError, search_article_ids, search_story_ids
from aggregator.sections import in_section

from . import api, API_VERSION, serializers as ser, settings
from .auth import redeem_pairing_code, require_token
from .errors import ApiError
from .params import (MAX_PAGE_SIZE, boolean, bad, cursor, encode_cursor, enum, integer, json_body,
                     query_args, text as text_param)
from .ratelimit import check_pairing

RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
DEFAULT_PAGE_SIZE = 20

def _story_load():
    """Load options for serializing stories without a query per article.
    (A function: Article.outlet is a backref, defined once mappers configure.)"""
    return (
        selectinload(Story.articles).joinedload(Article.outlet),
        selectinload(Story.articles).selectinload(Article.topics),
        selectinload(Story.topics),
    )


def _page(items, offset, limit):
    """The response for one page: items, plus next_cursor when there are more
    (callers fetch limit + 1 rows to tell)."""
    more = len(items) > limit
    return {"items": items[:limit], "next_cursor": encode_cursor(offset + limit) if more else None}


def _annotate_read(cards):
    """Set is_read on serialized story cards for this user (aggregator/reads.py)."""
    read = reads.read_ids(g.user.id, "story", [c["id"] for c in cards])
    for card in cards:
        card["is_read"] = card["id"] in read
    return cards


def _cutoff(range_key):
    window = RANGES[range_key]
    return datetime.utcnow() - window if window is not None else None


@api.route("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        db.session.rollback()
        return jsonify({"ok": False, "version": API_VERSION}), 503
    return jsonify({"ok": True, "version": API_VERSION})


@api.route("/devices/pair", methods=["POST"])
def pair_device():
    check_pairing()
    body = json_body(code=text_param(40), device_name=text_param(80))
    if not body["code"]:
        raise bad("code is required.")
    result = redeem_pairing_code(body["code"], body["device_name"], request.remote_addr)
    if result is None:
        raise ApiError(400, "invalid_code", "That code is wrong, used or expired. Create a new one in Settings.")
    token, device = result
    return jsonify({
        "token": token,
        "device": ser.device(device),
        "user": {"username": device.user.username},
    }), 201


@api.route("/me")
@require_token()
def me():
    return jsonify({
        "user": {"username": g.user.username, "role": g.user.role},
        "device": ser.device(g.device),
    })


@api.route("/me/devices/current", methods=["DELETE"])
@require_token()
def sign_out_device():
    g.device.revoked_at = datetime.utcnow()
    db.session.commit()
    return "", 204


# The feed defaults the profile page edits (aggregator/feed_prefs.py), minus
# the scrape-status filter, which is an admin's diagnostic.
PREFERENCE_FIELDS = ("start_page", "range", "hide_single")


def _preferences(prefs):
    return {name: prefs[name] for name in PREFERENCE_FIELDS}


@api.route("/me/preferences")
@require_token()
def get_preferences():
    return jsonify(_preferences(feed_prefs.load(g.user)))


@api.route("/me/preferences", methods=["PUT"])
@require_token()
def put_preferences():
    def flag(value):
        if not isinstance(value, bool):
            raise bad("Must be true or false.")
        return value

    body = json_body(start_page=enum(*feed_prefs.START_PAGES), range=enum(*feed_prefs.RANGES), hide_single=flag)
    prefs = feed_prefs.load(g.user)
    prefs.update({k: v for k, v in body.items() if v is not None})
    feed_prefs.save(g.user, prefs)
    return jsonify(_preferences(feed_prefs.load(g.user)))


@api.route("/me/boosted-sources")
@require_token()
def get_boosted_sources():
    """The outlets this reader boosts (aggregator/source_priority.py): their
    stories show in Trending alone, and first in Latest and the Swipe deck."""
    return jsonify({"boosted": source_priority.effective_names(g.user.id)})


@api.route("/me/boosted-sources", methods=["PUT"])
@require_token()
def put_boosted_source():
    """Boost (or stop boosting) one outlet for this reader: {"name", "boosted"}."""
    def outlet(value):
        if not isinstance(value, str) or not value.strip():
            raise bad("Must be an outlet name.")
        return value.strip()[:120]

    def flag(value):
        if not isinstance(value, bool):
            raise bad("Must be true or false.")
        return value

    body = json_body(name=outlet, boosted=flag)
    if body["name"] is None or body["boosted"] is None:
        raise bad("Send name and boosted.")
    names = source_priority.set_boost(g.user.id, body["name"], body["boosted"])
    return jsonify({"boosted": names})


@api.route("/topics")
@require_token()
def topics():
    rows = Topic.query.filter_by(is_active=True).order_by(Topic.sort_order, Topic.name).all()
    return jsonify({"items": [ser.topic(t) for t in rows]})


def _published_editions():
    return Edition.query.filter_by(published=True)


def _edition_payload(edition):
    rows = (EditionStory.query.filter_by(edition_id=edition.id)
            .options(joinedload(EditionStory.story).options(*_story_load()))
            .order_by(EditionStory.rank).all())
    out = ser.edition(edition, rows)
    _annotate_read(out["stories"])
    return out


@api.route("/editions")
@require_token()
def editions():
    args = query_args(cursor=cursor, limit=integer(1, MAX_PAGE_SIZE))
    offset, limit = args["cursor"] or 0, args["limit"] or DEFAULT_PAGE_SIZE
    rows = (_published_editions().order_by(Edition.date.desc(), Edition.created_at.desc())
            .offset(offset).limit(limit + 1).all())
    counts = dict(db.session.query(EditionStory.edition_id, func.count(EditionStory.id))
                  .filter(EditionStory.edition_id.in_([e.id for e in rows]))
                  .group_by(EditionStory.edition_id).all()) if rows else {}
    return jsonify(_page([ser.edition_brief(e, counts.get(e.id, 0)) for e in rows], offset, limit))


@api.route("/editions/latest")
@require_token()
def latest_edition():
    query_args()
    edition = _published_editions().order_by(Edition.created_at.desc()).first()
    if edition is None:
        raise ApiError(404, "not_found", "No edition has been published yet.")
    return jsonify(_edition_payload(edition))


@api.route("/editions/<int:edition_id>")
@require_token()
def edition_by_id(edition_id):
    query_args()
    edition = _published_editions().filter_by(id=edition_id).first()
    if edition is None:
        raise ApiError(404, "not_found", "Not found.")
    return jsonify(_edition_payload(edition))


@api.route("/opinion")
@require_token()
def opinion():
    args = query_args(edition=integer(1, 2**31 - 1))
    editions_q = _published_editions()
    edition = (editions_q.filter_by(id=args["edition"]).first() if args["edition"]
               else editions_q.order_by(Edition.created_at.desc()).first())
    if edition is None:
        raise ApiError(404, "not_found", "Not found.")
    cards = published_edition_opinion_cards(edition)
    return jsonify({
        "edition": ser.edition_brief(edition),
        "items": [
            {"story": ser.story_summary(story),
             "columns": ser.by_leaning(pieces, lambda p: ser.leaning(p.bias_score), ser.opinion_piece)}
            for story, pieces in cards
        ],
    })


@api.route("/stories")
@require_token()
def stories():
    """The story feed, filtered like All Stories: stories with an article
    published in the range, newest article first. A parameter left out
    falls back to the user's feed defaults."""
    args = query_args(topic=text_param(80), range=enum(*RANGES), single=boolean,
                      cursor=cursor, limit=integer(1, MAX_PAGE_SIZE))
    prefs = feed_prefs.load(g.user)
    range_key = args["range"] or prefs["range"]
    show_single = args["single"] if args["single"] is not None else not prefs["hide_single"]
    offset, limit = args["cursor"] or 0, args["limit"] or DEFAULT_PAGE_SIZE

    query = Story.query.join(Article).group_by(Story.id)
    cutoff = _cutoff(range_key)
    if cutoff is not None:
        query = query.filter(Story.articles.any(Article.date >= cutoff))
    if not show_single:
        query = query.having(func.count(Article.id) > 1)
    if args["topic"]:
        topic = Topic.query.filter_by(name=args["topic"]).first()
        if topic is None:
            raise ApiError(404, "not_found", "No such topic.")
        query = query.filter(in_section(topic))
    rows = (query.options(*_story_load()).order_by(func.max(Article.date).desc(), Story.id.desc())
            .offset(offset).limit(limit + 1).all())
    cards = _annotate_read([ser.story_summary(s) for s in rows])
    whys = headline_views.recent_whys([c["id"] for c in cards])
    now = datetime.utcnow()
    for story, card in zip(rows, cards):
        card["why"] = whys.get(card["id"])
        card["arrivals"] = ser.arrivals(story, now) if card["why"] else None
    return jsonify(_page(cards, offset, limit))


@api.route("/stories/<int:story_id>")
@require_token()
def story(story_id):
    query_args()
    row = Story.query.options(*_story_load()).get(story_id)
    if row is None:
        raise ApiError(404, "not_found", "Not found.")
    pieces = OpinionPiece.query.options(joinedload(OpinionPiece.outlet)).filter_by(story_id=story_id).all()
    out = _annotate_read([ser.story_detail(row, pieces)])[0]
    out["is_skipped"] = bool(skips.skipped_ids(g.user.id, [story_id]))
    return jsonify(out)


@api.route("/articles/<int:article_id>")
@require_token()
def article(article_id):
    query_args()
    return jsonify(article_payload(load_article(article_id)))


def load_article(article_id):
    row = Article.query.options(joinedload(Article.outlet), selectinload(Article.topics)).get(article_id)
    if row is None:
        raise ApiError(404, "not_found", "Not found.")
    return row


def article_payload(row):
    """/articles/{id} for this user: read state, and which of its summary and
    lean rating are still being written (aggregator/article_jobs.py)."""
    out = ser.article_detail(row, full_text=settings.load()["full_text"])
    out["is_read"] = reads.get(g.user.id, "article", row.id) is not None
    waiting = article_jobs.pending(row.id)
    out["is_summarizing"] = article_jobs.SUMMARY_JOB in waiting
    out["is_rating_bias"] = article_jobs.BIAS_JOB in waiting
    own = article_lean.latest(row.id, row.bias_source) if row.bias_source else None
    out["lean_source"] = row.bias_source or "outlet"
    out["lean_reason"] = own.reason if own is not None else None
    return out


@api.route("/search")
@require_token()
def search():
    """Compact hits for as-you-type search: stories or articles, best first."""
    args = query_args(q=text_param(), kind=enum("stories", "articles"), range=enum(*RANGES),
                      limit=integer(1, MAX_PAGE_SIZE))
    if not args["q"]:
        raise bad("q is required.")
    kind = args["kind"] or "stories"
    limit = args["limit"] or 10
    since = _cutoff(args["range"] or "all")
    try:
        if kind == "stories":
            ids = search_story_ids(args["q"], limit=limit, since=since)
            rows = Story.query.options(*_story_load()).filter(Story.id.in_(ids)).all() if ids else []
            serialize = ser.search_story_hit
        else:
            ids = search_article_ids(args["q"], limit=limit, since=since)
            rows = Article.query.options(joinedload(Article.outlet)).filter(Article.id.in_(ids)).all() if ids else []
            serialize = ser.search_article_hit
    except SearchUnavailableError:
        raise ApiError(503, "search_unavailable", "Search isn't available right now.")
    by_id = {row.id: row for row in rows}
    return jsonify({"items": [serialize(by_id[i]) for i in ids if i in by_id]})
