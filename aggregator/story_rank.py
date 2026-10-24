"""Built-in headline ranking: Story.headline_score without the private plugin.

Upstream ranks stories with news_fetcher/headline_ranker.py, which is kept out
of the repository (.gitignore). Without it every score stayed 0, so the
edition's final sort by score did nothing and Headlines showed stories in
publish_edition()'s selection-pass order: both-sides stories, then up to five
right-leaning ones, then center, then left. On 2026-10-23 that put five
single-outlet right-leaning stories at the top (Philip, 2026-10-24).

The score rewards breadth and freshness, nothing an LLM has to judge:

    (outlets + both-sides bonus + half the outlets that reported in the last
     6 hours) x 0.5 ** (hours since the newest article / 12)

Outlets are distinct outlets among the story's original articles (copies of
another article in the story don't count, aggregator/article_copies.py). A
story with any article scores above 0, so publish_edition()'s score > 0
candidate filter keeps working.
"""
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, literal, or_
from sqlalchemy.orm import aliased

from aggregator import db
from aggregator.models import Article, Outlet, Story

# A story covered by both left- and right-leaning outlets.
BOTH_SIDES_BONUS = 2.0
# Weight of each outlet that reported in the last MOMENTUM_HOURS.
MOMENTUM_WEIGHT = 0.5
MOMENTUM_HOURS = 6
# Hours for a story's score to halve after its newest article.
HALF_LIFE_HOURS = 12.0
# Keeps a story with articles above publish_edition()'s score > 0 gate.
MIN_SCORE = 0.001
# The edition looks at stories created in the last 3 days.
RANK_WINDOW_DAYS = 3


def _signal_rows(story_ids=None, created_since=None, now=None):
    """One row per story: id, outlets, left/center/right outlets, outlets that
    reported in the last MOMENTUM_HOURS, newest article time."""
    now = now or datetime.utcnow()
    original = aliased(Article)
    outlet_key = case(
        (Article.outlet_id.isnot(None), func.concat(literal("o"), Article.outlet_id)),
        (func.trim(func.coalesce(Article.source, "")) != "",
         func.concat(literal("s"), func.lower(func.trim(Article.source)))),
        else_=None,
    )
    score = func.coalesce(Article.bias_score, Outlet.bias_score)

    def outlets_where(condition):
        return func.count(func.distinct(case((condition, outlet_key), else_=None)))

    query = (
        db.session.query(
            Article.story_id,
            func.count(func.distinct(outlet_key)).label("outlets"),
            outlets_where(score <= 2.5).label("left"),
            outlets_where(and_(score > 2.5, score <= 3.5)).label("center"),
            outlets_where(score > 3.5).label("right"),
            outlets_where(Article.fetched_at >= now - timedelta(hours=MOMENTUM_HOURS)).label("recent"),
            func.max(func.coalesce(Article.date, Article.fetched_at)).label("newest"),
        )
        .outerjoin(Outlet, Outlet.id == Article.outlet_id)
        .outerjoin(original, original.id == Article.copy_of_id)
        .filter(Article.story_id.isnot(None))
        # Drop copies whose original is in the same story. Spelled out so a
        # NULL original.story_id keeps the row instead of making it unknown.
        .filter(or_(original.id.is_(None), original.story_id.is_(None),
                    original.story_id != Article.story_id))
        .group_by(Article.story_id)
    )
    if story_ids is not None:
        query = query.filter(Article.story_id.in_(list(story_ids)))
    if created_since is not None:
        query = query.join(Story, Story.id == Article.story_id).filter(Story.created_at >= created_since)
    return query.all()


def score_signals(row, now=None):
    """The headline score for one _signal_rows() row."""
    now = now or datetime.utcnow()
    base = row.outlets or 0
    if row.left and row.right:
        base += BOTH_SIDES_BONUS
    base += MOMENTUM_WEIGHT * (row.recent or 0)
    newest = min(row.newest or now, now)
    age_hours = max(0.0, (now - newest).total_seconds() / 3600)
    return max(MIN_SCORE, base * 0.5 ** (age_hours / HALF_LIFE_HOURS))


def story_scores(story_ids=None, created_since=None, now=None):
    """{story_id: score} for the given stories, or every story created since
    `created_since`."""
    now = now or datetime.utcnow()
    return {row.story_id: score_signals(row, now)
            for row in _signal_rows(story_ids=story_ids, created_since=created_since, now=now)}


def rank_recent_stories(now=None):
    """Write Story.headline_score for every story created in the last
    RANK_WINDOW_DAYS. Run by the scheduler where the private ranking plugin
    would run, when that plugin isn't installed."""
    now = now or datetime.utcnow()
    scores = story_scores(created_since=now - timedelta(days=RANK_WINDOW_DAYS), now=now)
    if scores:
        db.session.bulk_update_mappings(Story, [{"id": sid, "headline_score": s} for sid, s in scores.items()])
    db.session.commit()
    return {"status": "ok", "ranker": "built-in", "stories_scored": len(scores)}
