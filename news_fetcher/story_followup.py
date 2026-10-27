# news_fetcher/story_followup.py
"""Follow-up searches for stories still running after a day.

NewsAPI's free plan returns search (/everything) results a day late, so the
scheduled polls use its live top headlines instead. The late search is still
good for one thing: a story that has kept going for 24 hours or more has a
day of coverage behind it that the search can now see, from outlets the
feeds and headlines missed. Each pipeline run picks up to FOLLOWUPS_PER_RUN
such stories, most outlets first, and searches NewsAPI for each by the
keywords of its headline. A story is searched at most once every
SEARCH_EVERY; stories that ran for less than MIN_SPAN aren't searched.

The results are kept as payloads like a poll's, so ingest_payloads() stores
them in the same run and grouping files them under their stories. Searches
use the "followup" kind of NewsAPI's budget, capped at a share of its daily
limit (news_fetcher/api_budget.py) so the scheduled polls keep theirs.
"""

import json
import logging
import re
from datetime import datetime, timedelta

from news_fetcher import api_budget

logger = logging.getLogger(__name__)

MIN_SPAN = timedelta(hours=24)       # first to latest article
ACTIVE_WITHIN = timedelta(hours=24)  # latest article at most this old
SEARCH_EVERY = timedelta(hours=24)
FOLLOWUPS_PER_RUN = 4
MAX_KEYWORDS = 4
TOPIC_NAME = "Story follow-ups"
SEARCHED_KEY = "story_followups_v1"
# The free plan searches about a month back.
MAX_LOOKBACK = timedelta(days=29)

_STOPWORDS = {
    "about", "above", "after", "again", "against", "amid", "among", "around", "before", "being", "below",
    "between", "could", "during", "first", "from", "have", "having", "here", "into", "just", "last",
    "least", "more", "most", "much", "news", "over", "said", "says", "should", "since", "some", "than",
    "that", "their", "them", "then", "there", "these", "they", "this", "those", "through", "under",
    "until", "upon", "what", "when", "where", "which", "while", "will", "with", "within", "would",
    "year", "years", "your", "today", "report", "reports", "update", "live", "latest", "breaking",
    "new", "calls", "call", "make", "makes", "take", "takes", "back", "still",
}


def _utcnow():
    return datetime.utcnow()


def keywords(title):
    """Up to MAX_KEYWORDS search words from a headline: names (capitalised
    words) first, then the longest other words, each once."""
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*[A-Za-z]", title or "")
    seen, names, others = set(), [], []
    for i, word in enumerate(words):
        bare = word.lower().removesuffix("'s")
        if bare in seen or bare in _STOPWORDS or len(bare) < 4:
            continue
        seen.add(bare)
        # A capital at the start of a headline says nothing.
        (names if word[0].isupper() and i > 0 else others).append(word.removesuffix("'s"))
    others.sort(key=len, reverse=True)
    return (names + others)[:MAX_KEYWORDS]


def search_query(title):
    """A NewsAPI search for the headline's keywords, or None when it has
    fewer than two."""
    words = keywords(title)
    if len(words) < 2:
        return None
    return " AND ".join(words)


def _searched():
    from aggregator.models import AppSetting
    row = AppSetting.query.filter_by(key=SEARCHED_KEY).first()
    try:
        return json.loads(row.value) if row and row.value else {}
    except ValueError:
        return {}


def _save_searched(searched, now):
    from aggregator import db
    from aggregator.models import AppSetting
    cutoff = now - timedelta(days=7)
    kept = {k: v for k, v in searched.items() if datetime.fromisoformat(v) > cutoff}
    row = AppSetting.query.filter_by(key=SEARCHED_KEY).first()
    if row is None:
        row = AppSetting(key=SEARCHED_KEY)
        db.session.add(row)
    row.value = json.dumps(kept)
    db.session.commit()


def candidates(now=None, limit=40):
    """Stories that have run for MIN_SPAN and had an article within
    ACTIVE_WITHIN, as (story, first article time), most outlets first."""
    import sqlalchemy as sa
    from aggregator import db
    from aggregator.models import Article, Story
    now = now or _utcnow()
    when = sa.func.coalesce(Article.date, Article.fetched_at)
    first, latest = sa.func.min(when), sa.func.max(when)
    rows = (
        db.session.query(Article.story_id, first, sa.func.count(sa.distinct(Article.outlet_id)), latest)
        .filter(Article.story_id.isnot(None))
        .group_by(Article.story_id)
        .having(sa.and_(latest >= now - ACTIVE_WITHIN, latest - first >= MIN_SPAN))
        .order_by(sa.func.count(sa.distinct(Article.outlet_id)).desc(), latest.desc())
        .limit(limit)
        .all()
    )
    stories = {s.id: s for s in Story.query.filter(Story.id.in_([r[0] for r in rows])).all()} if rows else {}
    return [(stories[r[0]], r[1]) for r in rows if r[0] in stories]


def search_followups(now=None, per_run=FOLLOWUPS_PER_RUN):
    """Search NewsAPI for up to `per_run` long-running stories not searched
    in the last SEARCH_EVERY, keeping each result as a payload for
    ingest_payloads(). Returns metrics."""
    from aggregator import db
    from news_fetcher.fetch_and_store_articles.providers import (
        NEWSAPI_EVERYTHING_URL, _articles_of, _save_payload,
    )
    now = now or _utcnow()
    metrics = {"searched": 0, "articles": 0, "errors": 0, "status": "ok"}
    if not api_budget.enabled("newsapi"):
        metrics["status"] = "skipped"
        return metrics
    searched = _searched()
    due = [
        (story, first) for story, first in candidates(now)
        if not (searched.get(str(story.id)) and now - datetime.fromisoformat(searched[str(story.id)]) < SEARCH_EVERY)
    ]
    for story, first in due[:per_run]:
        query = search_query(story.headline or story.title)
        searched[str(story.id)] = now.isoformat()
        if not query:
            continue
        since = max(first - timedelta(days=1), now - MAX_LOOKBACK)
        params = {"q": query, "searchIn": "title,description", "language": "en", "sortBy": "relevancy",
                  "from": since.strftime("%Y-%m-%dT%H:%M:%S"), "pageSize": 50}
        key = api_budget.api_key("newsapi")
        try:
            body = api_budget.request("newsapi", "followup", NEWSAPI_EVERYTHING_URL, params=params,
                                      headers={"X-Api-Key": key}, key=key, items_of=_articles_of)
        except api_budget.Unavailable as e:
            logger.info(f"[Follow-up] Stopped: {e}")
            searched.pop(str(story.id), None)
            metrics["status"] = "unavailable"
            break
        except Exception as e:
            logger.warning(f"[Follow-up] Search for story {story.id} failed: {e}")
            metrics["errors"] += 1
            continue
        metrics["searched"] += 1
        if body:
            metrics["articles"] += _articles_of(body)
            _save_payload("newsapi", TOPIC_NAME, body)
            db.session.commit()
        logger.info(f"[Follow-up] Story {story.id}: {query!r}, {_articles_of(body)} articles")
    _save_searched(searched, now)
    return metrics
