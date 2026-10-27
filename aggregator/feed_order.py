"""The order RSS feeds are fetched in, chosen on Settings › RSS feeds.

Most reliable first by default (Philip, 2026-10-27), so when a run is cut
short or the classifier budget runs out, the better-sourced feeds are already
in. The grade is the outlet's reporting level from aggregator/
outlet_reporting.py, looked up by the feed's label or, without one, the outlet
name its articles last came in under (RssFeed.source_name).
"""
from urllib.parse import urlparse

from aggregator.outlet_reporting import LEVELS

SETTING_KEY = "rss_scan_order_v1"
RELIABILITY, ALPHABETICAL, ADDED = "reliability", "alphabetical", "added"
DEFAULT = RELIABILITY
CHOICES = (
    (RELIABILITY, "Most reliable first", "By reporting grade, Very high to Low; ungraded feeds last"),
    (ALPHABETICAL, "Alphabetical", "By outlet name"),
    (ADDED, "Date added", "Oldest feed first"),
)
_KEYS = {key for key, _, _ in CHOICES}


def current():
    from aggregator.models import AppSetting
    row = AppSetting.query.filter_by(key=SETTING_KEY).first()
    value = (row.value or "").strip('"') if row else ""
    return value if value in _KEYS else DEFAULT


def save(order):
    from aggregator.models import AppSetting, db
    if order not in _KEYS:
        return False
    row = AppSetting.query.filter_by(key=SETTING_KEY).first()
    if row is None:
        db.session.add(AppSetting(key=SETTING_KEY, value=order))
    else:
        row.value = order
    db.session.commit()
    return True


def feed_name(feed):
    """The name a feed sorts and shows under: label, outlet, else host."""
    return feed.label or feed.source_name or urlparse(feed.url).netloc.removeprefix("www.") or feed.url


def _added(feed):
    return (feed.added_at is None, feed.added_at or 0, feed.id or 0)


def sort_feeds(feeds, order=None):
    order = order or current()
    if order == ADDED:
        return sorted(feeds, key=_added)
    if order == ALPHABETICAL:
        return sorted(feeds, key=lambda f: (feed_name(f).lower(), f.id or 0))
    rank = {level: i for i, level in enumerate(LEVELS)}
    return sorted(feeds, key=lambda f: (rank.get(f.reporting, len(LEVELS)), feed_name(f).lower(), f.id or 0))
