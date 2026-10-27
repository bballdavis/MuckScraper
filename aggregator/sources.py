"""Which sections a source feeds (Settings › Sources).

Sections (the `topics` table) are the one list everything hangs off. A
source is an RSS feed or a NewsAPI/GNews search (ScheduledFetch). Each one
serves one or more sections, or none, which makes it General:

- An article from a source that serves sections is tagged with those
  sections directly; the classifier is not asked. BBC Sport's articles are
  Sports, whatever their first 200 characters say.
- An article from a General source is classified as before
  (news_fetcher/topic_classifier.py).

A search can also keep only outlets already in the database: the broad
Sports and World searches brought in about fifty one-article sites in a
night, Freerepublic.com and "Magic: The Gathering" among them (2026-10-27).

Feed sections are keyed by the feed's URL, not the RssFeed row, so a URL in
more than one bucket shares one set.
"""
from aggregator.models import FeedSection, RssFeed, ScheduledFetch, Topic, db

# What the RssFeed buckets mean, as the Sources page names them.
ROLES = {
    "general": "News",
    "op_ed": "Opinion",
    "left_enrichment": "Balance: left",
    "right_enrichment": "Balance: right",
}
ROLE_HINTS = {
    "general": "Read every run and every few minutes for Breaking; articles go into stories and sections",
    "op_ed": "Goes to Opinion & Editorials, never the news",
    "left_enrichment": "Searched for coverage when a story's sources lean right",
    "right_enrichment": "Searched for coverage when a story's sources lean left",
}


class IngestSections:
    """What store_articles() needs to know about sources, loaded once per
    batch. `label` is the batch's topic_name: a ScheduledFetch label for
    NewsAPI/GNews batches, "Global News" for feeds."""

    def __init__(self, label=None):
        active = {t.id: t.name for t in Topic.query.filter_by(is_active=True)}
        self.by_feed = {}
        for row in FeedSection.query.all():
            if row.topic_id in active:
                self.by_feed.setdefault(row.feed_url, []).append(active[row.topic_id])
        search = ScheduledFetch.query.filter_by(label=label).first() if label else None
        self.search_sections = [active[search.topic_id]] if search and search.topic_id in active else []
        self.known_outlets_only = bool(search and search.known_outlets_only)

    def sections_for(self, article):
        """Section names for a normalized article dict; [] means classify it."""
        return self.by_feed.get(article.get("feed_url") or "") or self.search_sections


def sections_by_feed():
    """{feed url: [Topic, ...]} for the Sources page, every section."""
    out = {}
    for row in FeedSection.query.join(Topic).order_by(Topic.sort_order.asc().nullslast(), Topic.name).all():
        out.setdefault(row.feed_url, []).append(row.topic)
    return out


def set_feed_sections(feed_url, topic_ids):
    """Replace the sections `feed_url` serves. Doesn't commit."""
    topic_ids = {int(t) for t in topic_ids}
    existing = {row.topic_id: row for row in FeedSection.query.filter_by(feed_url=feed_url)}
    for topic_id, row in existing.items():
        if topic_id not in topic_ids:
            db.session.delete(row)
    for topic_id in topic_ids - set(existing):
        if db.session.get(Topic, topic_id) is not None:
            db.session.add(FeedSection(feed_url=feed_url, topic_id=topic_id))


def section_source_counts():
    """{topic id: number of enabled feeds and active searches serving it}."""
    counts = {}
    enabled = {url for (url,) in db.session.query(RssFeed.url).filter(RssFeed.enabled.is_(True)).distinct()}
    for row in FeedSection.query.all():
        if row.feed_url in enabled:
            counts[row.topic_id] = counts.get(row.topic_id, 0) + 1
    for search in ScheduledFetch.query.filter(ScheduledFetch.is_active.is_(True), ScheduledFetch.topic_id.isnot(None)):
        counts[search.topic_id] = counts.get(search.topic_id, 0) + 1
    return counts
