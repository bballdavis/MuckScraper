"""Op/Ed columns linked to a news story, for the story page and the Op/Ed
page's "By story" view (2026-10-10).

The links and roundups are written by news_fetcher/opinion_linker.py during
full runs; this module only reads them.
"""
import re

from sqlalchemy import func, or_

from aggregator.models import Edition, EditionStory, OpinionPiece, Story, db

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


def edition_opinion_window(edition):
    """(start, end) of the Op/Ed links that belong to an edition.

    Columns are linked in the full run that publishes the edition, after it
    is published (opinion_linker runs after process_current_edition), so a
    column belongs to the edition whose created_at is the latest one before
    its link_checked_at. `end` is the next published edition's created_at, or
    None for the newest edition.
    """
    following = (Edition.query
                 .filter(Edition.published.is_(True), Edition.created_at > edition.created_at)
                 .order_by(Edition.created_at)
                 .first())
    return edition.created_at, (following.created_at if following else None)


def edition_opinion_cards(edition):
    """[(Story, [OpinionPiece])] for the stories that got new columns in this
    edition, most new columns first, then newest column. Pieces are ordered
    left, center, unrated, right, newest first within a side."""
    start, end = edition_opinion_window(edition)
    query = OpinionPiece.query.filter(OpinionPiece.story_id.isnot(None),
                                      OpinionPiece.link_checked_at >= start)
    if end is not None:
        query = query.filter(OpinionPiece.link_checked_at < end)
    by_story = {}
    for piece in query.all():
        by_story.setdefault(piece.story_id, []).append(piece)
    if not by_story:
        return []
    stories = {s.id: s for s in Story.query.filter(Story.id.in_(list(by_story))).all()}
    order = {"left": 0, "center": 1, "unrated": 2, "right": 3}

    def newest(piece):
        return piece.date or piece.fetched_at

    cards = []
    for story_id, pieces in by_story.items():
        story = stories.get(story_id)
        if story is None:
            continue
        pieces.sort(key=newest, reverse=True)
        pieces.sort(key=lambda p: order[leaning(p.bias_score)])
        cards.append((story, pieces))
    cards.sort(key=lambda card: (len(card[1]), max(newest(p) for p in card[1])), reverse=True)
    return cards


def stories_published_by(edition, story_ids):
    """The ids among story_ids that ran in a published edition up to and
    including this one."""
    if not story_ids:
        return set()
    rows = (db.session.query(EditionStory.story_id)
            .join(Edition, Edition.id == EditionStory.edition_id)
            .filter(EditionStory.story_id.in_(list(story_ids)),
                    Edition.published.is_(True),
                    Edition.created_at <= edition.created_at)
            .distinct().all())
    return {row[0] for row in rows}


def published_edition_opinion_cards(edition):
    """edition_opinion_cards() limited to stories that ran in this edition or
    an earlier one (columns about stories that never made
    an edition stay off the edition's Opinion page)."""
    cards = edition_opinion_cards(edition)
    published = stories_published_by(edition, {story.id for story, _ in cards})
    return [(story, pieces) for story, pieces in cards if story.id in published]


class OpinionCard:
    """An OpinionPiece shaped like a one-article Story, so the Op/Ed page's
    Latest list draws it with the same compact story_card as a single-article
    story on All Stories. The card leaves out the Breaking
    tag and the AI actions for it (`is_opinion`)."""

    is_opinion = True
    has_ai_headline = False
    edition_has_updates = False
    lead_image_focus = None

    def __init__(self, piece):
        self.piece = piece
        self.id = piece.id
        self.card_id = f"oped-{piece.id}"
        self.display_headline = piece.title
        self.last_updated = piece.date
        self.lead_image_url = piece.image_url
        self.story = piece.story
        # The one "article": the column itself, with the fields the card's
        # meta row, lean chip and ⋯ menu read.
        self.articles = self.display_articles = [piece]


def opinion_filters(q=None, since=None, side=None):
    """SQL conditions on OpinionPiece for the Op/Ed page's search, time range
    and outlet leaning ("left", "center", "right"), shared by its two views
    and the top bar's live results. The search matches the title, the
    outlet and the column's stance."""
    conditions = []
    if since is not None:
        conditions.append(OpinionPiece.date >= since)
    if side == "left":
        conditions.append(OpinionPiece.bias_score <= LEFT_MAX)
    elif side == "center":
        conditions.extend([OpinionPiece.bias_score > LEFT_MAX, OpinionPiece.bias_score <= CENTER_MAX])
    elif side == "right":
        conditions.append(OpinionPiece.bias_score > CENTER_MAX)
    if q:
        like = f"%{q}%"
        conditions.append(or_(OpinionPiece.title.ilike(like), OpinionPiece.source.ilike(like),
                              OpinionPiece.stance.ilike(like)))
    return conditions


def stories_with_opinion(limit=30, filters=()):
    """[(Story, columns)] for the stories with the most linked columns,
    newest column first among equals. `filters` (opinion_filters()) limits
    which columns count and are shown."""
    rows = (
        db.session.query(OpinionPiece.story_id, func.count(OpinionPiece.id), func.max(OpinionPiece.date))
        .filter(OpinionPiece.story_id.isnot(None), *filters)
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
    for piece in OpinionPiece.query.filter(OpinionPiece.story_id.in_(story_ids), *filters).all():
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
