"""Op/Ed columns linked to a news story, for the story page and the Op/Ed
page's "By story" view (2026-10-10).

The links and roundups are written by news_fetcher/opinion_linker.py during
full runs; this module only reads them.
"""
import re

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


# Scraped Op/Ed text is stored as one run of plain text (opinion_linker's
# _plain() drops the paragraph breaks), which is unreadable as a single block
# on the piece's page. Split it back at sentence ends into short paragraphs.
SENTENCES_PER_PARAGRAPH = 4
_SENTENCE_END_RE = re.compile(r'(\S*[.!?]["\u201d\u2019)]?)\s+(?=["\u201c\u2018(]?[A-Z0-9])')
# A full stop after these is not a sentence end ("Mr. Smith", "Sen. Paul").
_ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "sen.", "rep.", "gov.", "gen.", "lt.", "col.",
                  "jr.", "sr.", "no.", "vs.", "u.s.", "u.k.", "d.c.", "a.m.", "p.m.", "inc.", "co."}


def _split_sentences(text):
    def mark(match):
        word = match.group(1)
        if word.lower().lstrip("\"\u201c\u2018(") in _ABBREVIATIONS:
            return match.group(0)
        return word + "\0"
    return [part.strip() for part in _SENTENCE_END_RE.sub(mark, text).split("\0") if part.strip()]


def reading_paragraphs(text, per_paragraph=SENTENCES_PER_PARAGRAPH):
    """Paragraphs for display: the text's own blank-line breaks when it has
    them, otherwise groups of `per_paragraph` sentences."""
    text = (text or "").strip()
    if not text:
        return []
    if "\n" in text:
        return [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    sentences = _split_sentences(text)
    return [
        " ".join(sentences[i:i + per_paragraph])
        for i in range(0, len(sentences), per_paragraph)
    ]
