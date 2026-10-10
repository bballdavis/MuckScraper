"""Op/Ed columns linked to a news story, for the story page and the Op/Ed
page's "By story" view (2026-10-10).

The links and roundups are written by news_fetcher/opinion_linker.py during
full runs; this module only reads them.
"""
from sqlalchemy import func

from aggregator.models import OpinionPiece, Story, db

# Same three buckets as story_bias_totals() and the Op/Ed page filter.
LEFT_MAX = 2.5
CENTER_MAX = 3.5


def leaning(bias_score):
    """The outlet's leaning bucket. A column is filed by its outlet, not its
    argument; the roundup and each column's stance say what it argues."""
    if bias_score is None:
        return "unrated"
    if bias_score <= LEFT_MAX:
        return "left"
    if bias_score <= CENTER_MAX:
        return "center"
    return "right"


def split_by_leaning(pieces):
    columns = {"left": [], "center": [], "right": [], "unrated": []}
    for piece in sorted(pieces, key=lambda p: p.date or p.fetched_at, reverse=True):
        columns[leaning(piece.bias_score)].append(piece)
    return columns


def opinion_for_story(story_id):
    """{left, center, right, unrated: [OpinionPiece]} for one story, or None."""
    pieces = OpinionPiece.query.filter_by(story_id=story_id).all()
    return split_by_leaning(pieces) if pieces else None


def stories_with_opinion(limit=30):
    """[(Story, columns)] for the stories with the most linked columns,
    newest column first among equals."""
    rows = (
        db.session.query(OpinionPiece.story_id, func.count(OpinionPiece.id), func.max(OpinionPiece.date))
        .filter(OpinionPiece.story_id.isnot(None))
        .group_by(OpinionPiece.story_id)
        .order_by(func.count(OpinionPiece.id).desc(), func.max(OpinionPiece.date).desc().nullslast())
        .limit(limit)
        .all()
    )
    story_ids = [row[0] for row in rows]
    if not story_ids:
        return []
    stories = {s.id: s for s in Story.query.filter(Story.id.in_(story_ids)).all()}
    pieces = {}
    for piece in OpinionPiece.query.filter(OpinionPiece.story_id.in_(story_ids)).all():
        pieces.setdefault(piece.story_id, []).append(piece)
    return [(stories[sid], split_by_leaning(pieces[sid])) for sid in story_ids if sid in stories]
