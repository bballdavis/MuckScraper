"""Headlines' three views: Trending, Latest and Controversial (Philip,
2026-10-24). Read live from the database rather than from the twice-daily
edition, which still drives automatic summaries and stays under Past editions.

- Trending: 3+ outlets with an article in the last 24 hours, ranked by
  story_rank's headline score (outlets, both-sides bonus, recent outlets,
  12-hour half-life).
- Latest: every story with an article in the last 24 hours, single-source
  included, newest article first.
- Controversial: 2+ left- and 2+ right-leaning outlets in the last 48 hours,
  ranked by how differently the two sides write it up (the distance between
  the mean embeddings of their articles, the framing gap) plus linked op-eds,
  with a bonus when columns come from both sides.

Outlets are counted without copies (aggregator/article_copies.py), and lean
is the article's score, else its outlet's, like story_rank.
"""
from collections import namedtuple
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy.orm import selectinload

from aggregator import db
from aggregator.article_copies import original_articles
from aggregator.article_signals import bias_bucket_for_score
from aggregator.models import Article, OpinionPiece, Story
from aggregator.story_rank import _signal_rows, score_signals

VIEWS = ("trending", "latest", "controversial")
DEFAULT_VIEW = "trending"

LABELS = {
    "trending": ("Trending", "3+ outlets in the last 24h"),
    "latest": ("Latest", "Everything, newest first"),
    "controversial": ("Controversial", "Both sides, told differently"),
}

WINDOW_HOURS = 24
CONTROVERSIAL_WINDOW_HOURS = 48
TRENDING_MIN_OUTLETS = 3
CONTROVERSIAL_MIN_PER_SIDE = 2
LATEST_LIMIT = 150
# Same floor as the edition dedupe: blocked or paywalled articles have
# near-empty content and degenerate embeddings.
EMBEDDING_CONTENT_FLOOR = 500
# Controversy = GAP_WEIGHT x framing gap + OPED_WEIGHT x op-eds (at most
# OPED_CAP) + BOTH_SIDES_OPED_BONUS, halved every CONTROVERSY_HALF_LIFE_HOURS.
GAP_WEIGHT = 20.0
OPED_WEIGHT = 0.5
OPED_CAP = 10
BOTH_SIDES_OPED_BONUS = 1.0
CONTROVERSY_HALF_LIFE_HOURS = 24.0

# One listed story: the signals row, plus the controversy inputs when known.
Entry = namedtuple("Entry", "story_id signals score gap opeds")


def _side(score):
    bucket = bias_bucket_for_score(score)
    if bucket in ("left", "lean_left"):
        return "left"
    if bucket in ("right", "lean_right"):
        return "right"
    return None


def _unit_mean(vectors):
    if not vectors:
        return None
    mean = np.mean(vectors, axis=0)
    norm = np.linalg.norm(mean)
    return mean / norm if norm else None


def framing_gaps(story_ids):
    """{story_id: 1 - cosine(left centroid, right centroid)} for stories whose
    readable articles have embeddings on both sides."""
    if not story_ids:
        return {}
    stories = (Story.query.filter(Story.id.in_(list(story_ids)))
               .options(selectinload(Story.articles).selectinload(Article.outlet)).all())
    gaps = {}
    for story in stories:
        sides = {"left": [], "right": []}
        for article in original_articles(story.articles):
            if article.embedding is None or len(article.content or "") < EMBEDDING_CONTENT_FLOOR:
                continue
            score = article.bias_score
            if score is None and article.outlet:
                score = article.outlet.bias_score
            side = _side(score)
            if side:
                sides[side].append(np.asarray(article.embedding, dtype=float))
        left, right = _unit_mean(sides["left"]), _unit_mean(sides["right"])
        if left is not None and right is not None:
            gaps[story.id] = max(0.0, 1.0 - float(left @ right))
    return gaps


def opinion_counts(story_ids):
    """{story_id: (columns, from both sides)} for linked op-eds."""
    if not story_ids:
        return {}
    rows = (db.session.query(OpinionPiece.story_id, OpinionPiece.bias_score)
            .filter(OpinionPiece.story_id.in_(list(story_ids))).all())
    counts = {}
    for story_id, score in rows:
        total, sides = counts.get(story_id, (0, set()))
        side = _side(score)
        counts[story_id] = (total + 1, sides | ({side} if side else set()))
    return {sid: (total, sides == {"left", "right"}) for sid, (total, sides) in counts.items()}


def controversy_score(row, gap, opeds, now):
    columns, both_sides = opeds
    base = GAP_WEIGHT * (gap or 0.0) + OPED_WEIGHT * min(columns, OPED_CAP)
    if both_sides:
        base += BOTH_SIDES_OPED_BONUS
    # Coverage from both sides alone keeps a story listed, below any with a
    # measured gap or columns.
    base += 0.1 * min(row.left, row.right)
    newest = min(row.newest or now, now)
    age_hours = max(0.0, (now - newest).total_seconds() / 3600)
    return base * 0.5 ** (age_hours / CONTROVERSY_HALF_LIFE_HOURS)


def build(now=None, hidden_ids=frozenset()):
    """{view: [Entry]} for all three views, in display order, leaving out
    `hidden_ids` (stories the reader marked read)."""
    now = now or datetime.utcnow()
    rows = [r for r in _signal_rows(active_since=now - timedelta(hours=CONTROVERSIAL_WINDOW_HOURS), now=now)
            if r.story_id not in hidden_ids]
    day = now - timedelta(hours=WINDOW_HOURS)
    recent = [r for r in rows if r.newest and r.newest >= day]

    trending = [Entry(r.story_id, r, score_signals(r, now), None, None)
                for r in recent if r.outlets >= TRENDING_MIN_OUTLETS]
    trending.sort(key=lambda e: e.score, reverse=True)

    latest = [Entry(r.story_id, r, None, None, None) for r in recent]
    latest.sort(key=lambda e: e.signals.newest, reverse=True)

    both = [r for r in rows if r.left >= CONTROVERSIAL_MIN_PER_SIDE and r.right >= CONTROVERSIAL_MIN_PER_SIDE]
    ids = [r.story_id for r in both]
    gaps, opeds = framing_gaps(ids), opinion_counts(ids)
    controversial = []
    for r in both:
        gap, columns = gaps.get(r.story_id), opeds.get(r.story_id, (0, False))
        controversial.append(Entry(r.story_id, r, controversy_score(r, gap, columns, now), gap, columns))
    controversial.sort(key=lambda e: e.score, reverse=True)

    return {"trending": trending, "latest": latest[:LATEST_LIMIT], "controversial": controversial}


def load_stories(entries):
    """The Story objects for `entries`, in order, with .rank_entry set."""
    ids = [e.story_id for e in entries]
    if not ids:
        return []
    stories = {s.id: s for s in Story.query.filter(Story.id.in_(ids))
               .options(selectinload(Story.articles).selectinload(Article.outlet)).all()}
    out = []
    for entry in entries:
        story = stories.get(entry.story_id)
        if story is not None:
            story.rank_entry = entry
            out.append(story)
    return out


def why_label(view, entry):
    """The short chip a card shows for why it is in the view, or None."""
    if view == "trending" and entry.signals.recent:
        return f"{entry.signals.recent} reporting in the last 6h"
    if view == "controversial":
        parts = []
        if entry.gap is not None:
            parts.append("Framing gap: " + gap_level(entry.gap))
        columns = entry.opeds[0] if entry.opeds else 0
        if columns:
            parts.append(f"{columns} op-ed{'s' if columns != 1 else ''}")
        return " · ".join(parts) or "Covered by both sides"
    return None


def gap_level(gap):
    if gap is None:
        return None
    return "high" if gap >= 0.15 else "moderate" if gap >= 0.07 else "low"


def short_why(view, entry):
    """why_label cut down for a phone row, where it sits beside the L/C/R
    pill: "4 outlets in 6h", "High gap · 2 op-eds", "Both sides"."""
    if view == "trending" and entry.signals.recent:
        return f"{entry.signals.recent} outlet{'s' if entry.signals.recent != 1 else ''} in 6h"
    if view == "controversial":
        parts = []
        level = gap_level(entry.gap)
        if level:
            parts.append(f"{level.capitalize()} gap")
        columns = entry.opeds[0] if entry.opeds else 0
        if columns:
            parts.append(f"{columns} op-ed{'s' if columns != 1 else ''}")
        return " · ".join(parts) or "Both sides"
    return None


def hour_group(newest, now):
    """Latest's section headers."""
    if newest >= now - timedelta(hours=1):
        return "Last hour"
    if newest >= now - timedelta(hours=6):
        return "Last 6 hours"
    return "Earlier"
