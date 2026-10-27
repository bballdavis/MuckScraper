"""Op/Ed: the opinion pieces ingestion keeps out of the news (2026-10-08).

Lists `opinion_pieces` (see OpinionPiece in models.py). Nothing is fetched
for this section; it shows what the regular fetches brought in. Each piece
has its own page (oped_piece, 2026-10-16), laid out like the article page:
what it argues, the story it is linked to, its text and the publisher link.
Open to signed-out visitors under the same PUBLIC_READ_ACCESS switch as All
Stories, and like them they get no scraped text, so the excerpt and the
piece's text are signed-in only.
"""
from datetime import datetime

from flask import render_template, request, url_for
from flask_login import current_user
from markupsafe import Markup
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from aggregator.constants import OPED_SECTION_NAME
from aggregator.models import OpinionPiece, Story, Topic, db
from aggregator.navigation import incoming_back
from aggregator.opinion import OpinionCard, leaning, opinion_filters, reading_paragraphs, stories_with_opinion
from aggregator.permissions import public_or_login
from aggregator.story_view import apply_aggregator_filter

from . import admin
from ._shared import story_bias_totals
from .articles import DEFAULT_TIME_RANGE, TIME_RANGES

PER_PAGE = 25

LEANINGS = [("left", "Left"), ("center", "Center"), ("right", "Right")]
TIME_RANGE_LABELS = [("24h", "24 hours"), ("7d", "7 days"), ("30d", "30 days"), ("all", "All time")]


@admin.route("/oped")
@public_or_login
def oped_list():
    """Built like the section pages (articles.html, Philip 2026-10-27): the
    top bar's search and Filters, the filter chips, and the same compact
    story_card. Latest draws each column as a one-article story
    (OpinionCard); By story lists the stories the columns are about, as a
    section does. The view is the Headlines-style switch under the page
    title (Philip, 2026-10-27). Search, time range and leaning apply to both."""
    page = max(1, request.args.get("page", 1, type=int) or 1)
    active_range = request.args.get("range", DEFAULT_TIME_RANGE)
    if active_range not in TIME_RANGES:
        active_range = DEFAULT_TIME_RANGE
    active_leaning = request.args.get("leaning", "").strip().lower() or None
    if active_leaning not in dict(LEANINGS):
        active_leaning = None
    active_search_query = request.args.get("q", "").strip() or None
    active_view = "story" if request.args.get("view") == "story" else "latest"

    window = TIME_RANGES[active_range]
    filters = opinion_filters(active_search_query,
                              datetime.utcnow() - window if window is not None else None,
                              active_leaning)
    query = OpinionPiece.query.filter(*filters)
    total = query.count()
    story_total = (db.session.query(func.count(func.distinct(OpinionPiece.story_id)))
                   .filter(OpinionPiece.story_id.isnot(None), *filters).scalar() or 0)

    pieces, by_story, total_pages = [], [], 1
    if active_view == "story":
        by_story = stories_with_opinion(filters=filters)
        for story, _columns in by_story:
            apply_aggregator_filter(story)
            story_bias_totals(story)
    else:
        total_pages = max(1, -(-total // PER_PAGE))
        page = min(page, total_pages)
        pieces = (
            query.options(joinedload(OpinionPiece.outlet), joinedload(OpinionPiece.story))
            .order_by(OpinionPiece.date.desc().nullslast(), OpinionPiece.id.desc())
            .offset((page - 1) * PER_PAGE)
            .limit(PER_PAGE)
            .all()
        )
    return render_template(
        "oped.html",
        section_title=OPED_SECTION_NAME,
        cards=[OpinionCard(piece) for piece in pieces],
        active_view=active_view,
        by_story=by_story,
        total=total,
        story_total=story_total,
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
        active_nav="oped",
        active_range=active_range,
        active_leaning=active_leaning,
        active_search_query=active_search_query,
        leanings=LEANINGS,
        time_ranges=TIME_RANGE_LABELS,
        page=page,
        total_pages=total_pages,
    )


@admin.route("/oped/<int:piece_id>")
@public_or_login
def oped_piece(piece_id):
    piece = OpinionPiece.query.get_or_404(piece_id)
    story = Story.query.get(piece.story_id) if piece.story_id else None
    # The scraped column when we have it, else the feed's teaser.
    if piece.full_text:
        text_kind, paragraphs, teaser_html = "full", reading_paragraphs(piece.full_text), None
    elif piece.content and Markup(piece.content).striptags().strip():
        text_kind, paragraphs, teaser_html = "teaser", [], piece.content
    else:
        text_kind, paragraphs, teaser_html = "none", [], None

    back = incoming_back()
    if back and back.startswith("/story/"):
        back_label = "Back to Story"
    else:
        back_label = f"Back to {OPED_SECTION_NAME}"
        back = back or url_for("admin.oped_list")
        if "#" not in back:
            back = f"{back}#oped-{piece.id}"

    return render_template(
        "oped_piece.html",
        piece=piece,
        story=story,
        side=leaning(piece.bias_score),
        text_kind=text_kind,
        paragraphs=paragraphs if current_user.is_authenticated else [],
        teaser_html=teaser_html if current_user.is_authenticated else None,
        back_href=back,
        back_label=back_label,
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
        active_nav="oped",
    )
