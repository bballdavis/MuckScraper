"""The reader's source browser. Uses the same account pins as the iOS API."""
from flask import Blueprint, render_template, request
from flask_login import current_user, login_required

from aggregator import pinned_sources

reader_sources = Blueprint("reader_sources", __name__)


@reader_sources.route("/sources")
@login_required
def index():
    query = request.args.get("q", "").strip()[:80]
    pinned = pinned_sources.outlets(current_user.id)
    found, page, pages, total = pinned_sources.browse(
        query, exclude_ids=[outlet.id for outlet in pinned],
        page=request.args.get("page", 1, type=int) or 1,
    )
    matches = [outlet for outlet in pinned if query.casefold() in outlet.name.casefold()]
    return render_template(
        "reader_sources.html", q=query, pinned=matches, candidates=found,
        pinned_count=len(pinned), pinned_max=pinned_sources.MAX_PINNED,
        total=total, page=page, total_pages=pages,
    )
