"""Which stories a section page lists.

A story collects every topic any of its articles was ever classified under
(news_fetcher/fetch_and_store_articles/ingestion.py), so a big story with
forty articles ends up in nearly every topic: one piece mentions the markets
and it is in Buss/Fin, one classifier miss and it is in Other. Filtering a
section by `Story.topics` alone therefore filled Sports, Sci/Tech and Other
with the same big stories Headlines leads with, newest first, and the
section looked like Headlines (Philip, 2026-10-27).

A story belongs in a section when at least a third of its articles carry the
topic themselves. A story whose articles carry no topics at all (older rows,
or ones from before articles were tagged) falls back to the story's own
topics, as before.
"""
from sqlalchemy import and_, func, or_, select

from aggregator.models import Article, Story, Topic, article_topics

# The share of a story's articles that must carry the topic, as 1/N.
SHARE_DIVISOR = 3


def in_section(topic):
    """SQL condition for Story: the story belongs in `topic`'s section.
    `topic` is a Topic or its id."""
    topic_id = getattr(topic, "id", topic)
    tagged = (select(func.count(article_topics.c.article_id))
              .join(Article, Article.id == article_topics.c.article_id)
              .where(Article.story_id == Story.id, article_topics.c.topic_id == topic_id)
              .correlate(Story).scalar_subquery())
    total = (select(func.count(Article.id)).where(Article.story_id == Story.id)
             .correlate(Story).scalar_subquery())
    any_tagged = (select(article_topics.c.article_id)
                  .join(Article, Article.id == article_topics.c.article_id)
                  .where(Article.story_id == Story.id)
                  .correlate(Story).exists())
    return and_(Story.topics.any(Topic.id == topic_id),
                or_(tagged * SHARE_DIVISOR >= total, ~any_tagged))


# Reader-facing section names (Settings › Sections), cached briefly: every
# page renders the sidebar, and names change only when an admin edits them.
_LABEL_TTL_SECONDS = 30
_labels = {"map": None, "expires_at": 0.0}


def _label_map():
    import time
    now = time.monotonic()
    if _labels["map"] is None or now >= _labels["expires_at"]:
        try:
            _labels["map"] = {name: display for name, display in
                              Topic.query.with_entities(Topic.name, Topic.display_name) if display}
        except Exception:
            from aggregator import db
            db.session.rollback()
            return {}
        _labels["expires_at"] = now + _LABEL_TTL_SECONDS
    return _labels["map"]


def forget_section_labels():
    """Drop the cache after an edit, so this process shows it at once."""
    _labels["map"] = None


def section_label(topic_name):
    """What readers see for a section key: its display name, else the
    built-in name (constants.SECTION_NAMES), else the key itself."""
    from aggregator.constants import section_name
    if not topic_name:
        return topic_name
    return _label_map().get(topic_name) or section_name(topic_name)
