"""Add default feeds for free nonprofit newsrooms whose full text can be read.

Revision ID: a8d3e6f1c2b7
Revises: c7f2a9e4d6b8
Create Date: 2026-10-22

NYT, WSJ and other paywalled outlets stay headline-and-abstract only (see the
note at the top of news_fetcher/scraper.py). These newsrooms have no paywall
and invite republishing under Creative Commons terms, so the scraper gets the
whole article and the summaries have real text to work from.

Texas Tribune's feed lives on its own feeds. host; it falls back to a Google
News search feed if that host stops answering.

Like f7a1d3c5e8b2, upgrade() only INSERTS rows that are missing and never
touches a feed an install already has. aggregator/seed_defaults.py carries the
same rows for a fresh install.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = 'a8d3e6f1c2b7'
down_revision = 'c7f2a9e4d6b8'
branch_labels = None
depends_on = None


def _google(site):
    return f"https://news.google.com/rss/search?q=site:{site}+when:1d&hl=en-US&gl=US&ceid=US:en"


# (url, bucket, label, fallback_url)
NEW_FEEDS = [
    ("https://feeds.texastribune.org/feeds/main/", "general", "The Texas Tribune",
     _google("texastribune.org")),
    ("https://www.propublica.org/feeds/propublica/main", "general", "ProPublica", None),
    ("https://calmatters.org/feed/", "general", "CalMatters", None),
    ("https://kffhealthnews.org/feed/", "general", "KFF Health News", None),
    ("https://insideclimatenews.org/feed/", "general", "Inside Climate News", None),
]


def upgrade():
    conn = op.get_bind()
    now = datetime.utcnow()
    for url, bucket, label, fallback_url in NEW_FEEDS:
        exists = conn.execute(
            sa.text("SELECT 1 FROM rss_feeds WHERE url = :url AND bucket = :bucket"),
            {"url": url, "bucket": bucket},
        ).first()
        if exists:
            continue
        conn.execute(
            sa.text("INSERT INTO rss_feeds (url, bucket, label, enabled, added_at, fallback_url) "
                    "VALUES (:url, :bucket, :label, true, :now, :fallback_url)"),
            {"url": url, "bucket": bucket, "label": label, "now": now, "fallback_url": fallback_url},
        )


def downgrade():
    # Inserted rows are indistinguishable from ones an admin added by hand.
    pass
