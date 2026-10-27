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
