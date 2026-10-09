"""Op/Ed: the opinion pieces ingestion keeps out of the news (2026-10-08).

Lists `opinion_pieces` (see OpinionPiece in models.py). Nothing is fetched
for this section; it shows what the regular fetches brought in. Each piece
links out to its publisher; there is no page of our own for one. Open to
signed-out visitors under the same PUBLIC_READ_ACCESS switch as All Stories,
and like them they get no scraped text, so the excerpt is signed-in only.
"""
from datetime import datetime

from flask import render_template, request
from flask_login import current_user
from markupsafe import Markup

from aggregator.models import OpinionPiece, Topic
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

    return render_template(
        "oped.html",
        pieces=pieces,
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
