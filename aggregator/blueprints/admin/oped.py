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

from aggregator.models import OpinionPiece, Story, Topic
from aggregator.navigation import incoming_back
from aggregator.opinion import leaning, reading_paragraphs, stories_with_opinion
from aggregator.permissions import public_or_login

from . import admin
from .articles import DEFAULT_TIME_RANGE, TIME_RANGES

PER_PAGE = 25
EXCERPT_CHARS = 280

# Same three buckets as story_bias_totals() and the search page:
# <= 2.5 left, <= 3.5 center, above that right.
LEANINGS = {
    "left": (None, 2.5),
    "center": (2.5, 3.5),
    "right": (3.5, None),
}


def _excerpt(content):
    text = " ".join(Markup(content or "").striptags().split())
    if len(text) <= EXCERPT_CHARS:
        return text
    return text[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"


@admin.route("/oped")
@public_or_login
def oped_list():
    page = max(1, request.args.get("page", 1, type=int) or 1)
    active_range = request.args.get("range", DEFAULT_TIME_RANGE)
    if active_range not in TIME_RANGES:
        active_range = DEFAULT_TIME_RANGE
    active_leaning = request.args.get("leaning", "").strip().lower() or None
    if active_leaning not in LEANINGS:
        active_leaning = None
    active_search_query = request.args.get("q", "").strip() or None

    query = OpinionPiece.query
    window = TIME_RANGES[active_range]
    if window is not None:
        query = query.filter(OpinionPiece.date >= datetime.utcnow() - window)
    if active_leaning:
        low, high = LEANINGS[active_leaning]
        if low is not None:
            query = query.filter(OpinionPiece.bias_score > low)
        if high is not None:
            query = query.filter(OpinionPiece.bias_score <= high)
    if active_search_query:
        like = f"%{active_search_query}%"
        query = query.filter(OpinionPiece.title.ilike(like) | OpinionPiece.source.ilike(like))

    total = query.count()
    total_pages = max(1, -(-total // PER_PAGE))
    page = min(page, total_pages)
    pieces = (
        query.order_by(OpinionPiece.date.desc().nullslast(), OpinionPiece.id.desc())
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
        .all()
    )
    excerpts = {p.id: _excerpt(p.content) for p in pieces} if current_user.is_authenticated else {}
    active_view = "story" if request.args.get("view") == "story" else "latest"
    by_story = []
    if active_view == "story":
        by_story = stories_with_opinion()

    return render_template(
        "oped.html",
        pieces=pieces,
        active_view=active_view,
        by_story=by_story,
        excerpts=excerpts,
        total=total,
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
        active_nav="oped",
        active_range=active_range,
        active_leaning=active_leaning,
        active_search_query=active_search_query,
        leanings=[("left", "Left"), ("center", "Center"), ("right", "Right")],
        time_ranges=[("24h", "24 hours"), ("7d", "7 days"), ("30d", "30 days"), ("all", "All time")],
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
        back_label = "Back to Op/Ed"
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
