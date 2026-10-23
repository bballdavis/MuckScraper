"""Outlet country and funding; default feeds for more US and international outlets.

Revision ID: e4a7c2f9b1d6
Revises: b3d8f1a6c2e5
Create Date: 2026-10-23

Non-US outlets with no AllSides rating stop being LLM-rated on the US
left/right scale and show their country instead (see
aggregator/outlet_origins.py). The columns start empty; apply_outlet_origins()
fills them on the next retry_unrated_outlets() pass.

Feeds checked from a desktop on 2026-10-23: each answered with fresh items
and articles that are free to read. Deseret News (empty feed), NHK World (no
English feed), The Free Press (metered paywall) and UPI, France 24, Sky News
and Times of Israel (article pages blocked to the scraper) were left out.

Like a8d3e6f1c2b7, upgrade() only INSERTS feeds that are missing.
aggregator/seed_defaults.py carries the same rows for a fresh install.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = 'e4a7c2f9b1d6'
down_revision = 'b3d8f1a6c2e5'
branch_labels = None
depends_on = None


def _google(site):
    return f"https://news.google.com/rss/search?q=site:{site}+when:1d&hl=en-US&gl=US&ceid=US:en"


# (url, bucket, label, fallback_url)
NEW_FEEDS = [
    ("https://www.semafor.com/rss.xml", "general", "Semafor", _google("semafor.com")),
    ("https://www.newsnationnow.com/feed/", "general", "NewsNation", None),
    ("https://www.courthousenews.com/feed/", "general", "Courthouse News Service", None),
    ("https://stateline.org/feed/", "general", "Stateline", None),
    ("https://19thnews.org/feed/", "general", "The 19th", None),
    ("https://www.thecentersquare.com/search/?f=rss&t=article&l=50&s=start_time&sd=desc",
     "general", "The Center Square", None),
    ("https://rss.dw.com/rdf/rss-en-all", "general", "Deutsche Welle", None),
    ("https://kyivindependent.com/news-archive/rss/", "general", "Kyiv Independent", None),
    ("https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", "general", "CNA", None),
    ("https://www.abc.net.au/news/feed/2942460/rss.xml", "general", "ABC News (Australia)", None),
    ("https://www.cbc.ca/webfeed/rss/rss-topstories", "general", "CBC News", None),
    ("https://www.dailymaverick.co.za/dmrss/", "general", "Daily Maverick", None),
]


def upgrade():
    op.add_column('outlets', sa.Column('country', sa.String(length=2), nullable=True))
    op.add_column('outlets', sa.Column('funding', sa.String(length=16), nullable=True))

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
    # Inserted feeds are indistinguishable from ones an admin added by hand.
    op.drop_column('outlets', 'funding')
    op.drop_column('outlets', 'country')
