"""How each RSS feed answered the last time it was read, for Settings › Sources.

The Breaking poller (news_fetcher/breaking_feed.py) reads every news feed
every few minutes and the pipeline reads the rest, so both record here. A
feed that comes back empty or fails NEEDS_ATTENTION_AFTER times in a row is
flagged on the Sources page; nothing is switched off on its own. CBC News and
Breitbart had answered with no items for days before anyone saw it
(2026-10-27).
"""
import logging
from datetime import datetime

from aggregator.models import db

STATUS_OK = "ok"
STATUS_FALLBACK = "fallback"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"

NEEDS_ATTENTION_AFTER = 3

logger = logging.getLogger(__name__)


def record_feed_read(feed_url, status, items=None, error=None, now=None):
    """Store one read of `feed_url` on every RssFeed row with that URL.
    `items` is the number of entries read (None when the feed answered
    304 Not Modified, which counts as a good read and keeps the last count).
    Its own connection, like rss_fetcher._remember_feed_outlet, so it never
    commits or rolls back a run's work; never raises, so a health write
    can't stop a fetch."""
    now = now or datetime.utcnow()
    bad = status in (STATUS_EMPTY, STATUS_ERROR)
    try:
        with db.engine.begin() as conn:
            conn.execute(db.text(
                "UPDATE rss_feeds SET last_read_at = :now, last_status = :status, last_error = :error, "
                "last_item_count = COALESCE(:items, last_item_count), "
                "failures_in_row = CASE WHEN :bad THEN COALESCE(failures_in_row, 0) + 1 ELSE 0 END "
                "WHERE url = :url"
            ), {"now": now, "status": status, "error": (error or "")[:300] or None,
                "items": items, "bad": bad, "url": feed_url})
    except Exception as e:
        logger.warning(f"[Feed health] Couldn't record a read of {feed_url[:60]}: {e}")


def health_of(feed):
    """'good', 'warning' (fallback, or one or two bad reads), 'bad' (needs
    attention) or 'unknown' (never read, or switched off)."""
    if not feed.enabled or feed.last_status is None:
        return "unknown"
    if (feed.failures_in_row or 0) >= NEEDS_ATTENTION_AFTER:
        return "bad"
    if feed.last_status == STATUS_FALLBACK or (feed.failures_in_row or 0) > 0:
        return "warning"
    return "good"


def health_note(feed):
    """One line for the health dot's tooltip."""
    if not feed.enabled:
        return "Switched off"
    if feed.last_status is None:
        return "Not read yet"
    failures = feed.failures_in_row or 0
    if feed.last_status == STATUS_ERROR:
        note = f"Failed: {feed.last_error or 'no answer'}"
    elif feed.last_status == STATUS_EMPTY:
        note = "Answered with no items"
    elif feed.last_status == STATUS_FALLBACK:
        note = f"Using its fallback feed ({feed.last_error or 'feed failed'})"
    else:
        note = f"{feed.last_item_count or 0} items in the last read"
    if failures >= 2:
        note += f" · {failures} bad reads in a row"
    return note
