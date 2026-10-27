"""How each RSS feed answered the last time it was read, for Settings › Sources.

The Breaking poller (news_fetcher/breaking_feed.py) reads every news feed
every few minutes and the pipeline reads the rest, so both record here. A
feed that comes back empty or fails NEEDS_ATTENTION_AFTER times in a row is
flagged on the Sources page; nothing is switched off on its own. CBC News and
Breitbart had answered with no items for days before anyone saw it
(2026-10-27).
"""
from datetime import datetime

from aggregator.models import RssFeed, db

STATUS_OK = "ok"
STATUS_FALLBACK = "fallback"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"

NEEDS_ATTENTION_AFTER = 3


def record_feed_read(feed_url, status, items=None, error=None, now=None):
    """Store one read of `feed_url` on every RssFeed row with that URL.
    `items` is the number of entries read (None when the feed answered
    304 Not Modified, which counts as a good read and keeps the last count).
    Commits; never raises, so a health write can't stop a fetch."""
    now = now or datetime.utcnow()
    try:
        rows = RssFeed.query.filter_by(url=feed_url).all()
        for row in rows:
            row.last_read_at = now
            row.last_status = status
            row.last_error = (error or "")[:300] or None
            if items is not None:
                row.last_item_count = items
            if status in (STATUS_EMPTY, STATUS_ERROR):
                row.failures_in_row = (row.failures_in_row or 0) + 1
            else:
                row.failures_in_row = 0
        db.session.commit()
    except Exception:
        db.session.rollback()


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
