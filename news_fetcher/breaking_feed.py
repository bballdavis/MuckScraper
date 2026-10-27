"""The Breaking poller: reads the general RSS feeds every
BREAKING_POLL_MINUTES (scheduler.py) and stores their headlines as
FeedEntries (aggregator/breaking.py). Headlines only: no scraping, no Google
News link lookups, no model call. Conditional GETs keep it cheap.

Then it decides which recent entries are breaking. An event counts when
MIN_OUTLETS or more different outlets carry it within CLUSTER_SPAN (matched
on shared title words, like the story grouper's quick check), or when a
feed marks it ("BREAKING:", "LIVE:", "Developing", "Live updates"), and it
doesn't match a story we already had before it was published. Each event
shows once, as the entry that reported it first.
"""
import logging
import os
import re
from datetime import datetime, timedelta

from aggregator import breaking, feed_health
from aggregator.models import FeedEntry, RssFeed, Story, db

logger = logging.getLogger(__name__)

POLL_MINUTES = max(1, int(os.environ.get("BREAKING_POLL_MINUTES", "5") or 5))
MIN_OUTLETS = 3
CLUSTER_SPAN = timedelta(minutes=90)
# Stories this old or newer, made before an event began, mean it isn't new.
STORY_LOOKBACK = timedelta(hours=24)
MAX_ENTRIES_PER_FEED = 30

_MARKER = re.compile(r"^\s*(breaking(\s+news)?|live|developing|just in)\b\s*[:|\-–—]|\blive updates\b", re.I)

# feed URL -> (etag, modified), for conditional GETs. Per process.
_validators = {}


def is_marked(title):
    return bool(_MARKER.search(title or ""))


def _tokens(title):
    from news_fetcher.story_grouper import normalize_title_tokens
    return normalize_title_tokens(_MARKER.sub("", title or ""))


def same_event(tokens_a, tokens_b):
    """Looser than titles_are_near_duplicates: outlets word a breaking event
    differently, but share its names and nouns."""
    shared = tokens_a & tokens_b
    if len(shared) < 3:
        return False
    return len(shared) / max(1, min(len(tokens_a), len(tokens_b))) >= 0.4


def cluster(entries):
    """Group entries (oldest first) into events. Returns a list of lists of
    entries; each list's first entry reported it first."""
    events = []
    for entry in sorted(entries, key=lambda e: (e.published_at, e.id or 0)):
        tokens = _tokens(entry.title)
        for event in events:
            first = event[0][0]
            if entry.published_at - first.published_at > CLUSTER_SPAN:
                continue
            if any(same_event(tokens, other) for _, other in event):
                event.append((entry, tokens))
                break
        else:
            events.append([(entry, tokens)])
    return [[e for e, _ in event] for event in events]


def _outlet_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _already_a_story(tokens, published_at, stories):
    """Whether a story made before this event began already covers it."""
    for story in stories:
        if story.created_at and story.created_at >= published_at:
            continue
        for title in (story.headline, story.title):
            if title and same_event(tokens, _tokens(title)):
                return True
    return False


def mark_breaking(now=None):
    """Recompute outlet_count, cluster_id and is_breaking for recent entries.
    Commits."""
    now = now or datetime.utcnow()
    since = now - breaking.WINDOW - CLUSTER_SPAN
    entries = FeedEntry.query.filter(FeedEntry.published_at >= since).all()
    stories = (Story.query.with_entities(Story.title, Story.headline, Story.created_at)
               .filter(Story.created_at >= since - STORY_LOOKBACK).all())
    flagged = 0
    for event in cluster(entries):
        first = event[0]
        outlets = {_outlet_key(e.source_name) for e in event}
        is_event = len(outlets) >= MIN_OUTLETS or any(is_marked(e.title) for e in event)
        is_breaking = (is_event and first.published_at >= now - breaking.WINDOW
                       and not _already_a_story(_tokens(first.title), first.published_at, stories))
        for e in event:
            e.cluster_id = first.id
            e.outlet_count = len(outlets)
            e.is_breaking = is_breaking and e is first
        flagged += int(is_breaking)
    db.session.commit()
    return flagged


def poll_feeds(now=None):
    """Read every enabled general feed, store new headlines, link the ones the
    pipeline has scraped since, flag breaking events and prune old entries.
    Returns a small metrics dict."""
    from news_fetcher.rss_fetcher import read_feed_headlines
    now = now or datetime.utcnow()
    feeds = {}
    for row in RssFeed.query.filter_by(bucket="general", enabled=True).all():
        feeds.setdefault(row.url, row.fallback_url)
    metrics = {"feeds": len(feeds), "unchanged": 0, "failed": 0, "new": 0}
    for url, fallback in feeds.items():
        etag, modified = _validators.get(url, (None, None))
        try:
            articles, read_url, etag, modified = read_feed_headlines(
                url, max_entries=MAX_ENTRIES_PER_FEED, fallback_url=fallback, etag=etag, modified=modified)
        except Exception as e:
            db.session.rollback()
            metrics["failed"] += 1
            logger.warning("[Breaking] %s failed: %s", url[:60], e)
            feed_health.record_feed_read(url, feed_health.STATUS_ERROR, error=str(e))
            continue
        if read_url == url:
            _validators[url] = (etag, modified)
        if articles is None:
            metrics["unchanged"] += 1
            feed_health.record_feed_read(url, feed_health.STATUS_OK)
            continue
        if read_url != url:
            status = feed_health.STATUS_FALLBACK
        else:
            status = feed_health.STATUS_OK if articles else feed_health.STATUS_EMPTY
        feed_health.record_feed_read(url, status, items=len(articles))
        metrics["new"] += breaking.store_headlines(articles, read_url, now)
    breaking.link_scraped(now)
    metrics["breaking"] = mark_breaking(now)
    breaking.prune(now)
    logger.info("[Breaking] Polled %(feeds)s feeds: %(new)s new headlines, %(unchanged)s unchanged, "
                "%(failed)s failed, %(breaking)s breaking", metrics)
    return metrics
