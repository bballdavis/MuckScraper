"""Default feeds for the thin sections: Sports, Sci/Tech, Buss/Fin, world and Other.

Revision ID: f3c8a1e6d2b9
Revises: a4b42504ce6b
Create Date: 2026-10-27

Sports, Sci/Tech, Buss/Fin and Other had almost no stories of their own:
every default feed is a general or politics feed (Philip, 2026-10-27). These
are section feeds from outlets already in the list (BBC, NPR, The Guardian)
plus topic outlets with public RSS.

Feeds checked from a desktop on 2026-10-27: each answered with fresh items,
and its newest article loaded with its full text. ESPN (feed blocked by a
bot challenge), Yahoo Finance (feed gone, 404) and MarketWatch (article pages
refused, 401) were left out.

Like e4a7c2f9b1d6, upgrade() only INSERTS feeds that are missing.
aggregator/seed_defaults.py carries the same rows for a fresh install.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = 'f3c8a1e6d2b9'
down_revision = 'a4b42504ce6b'
branch_labels = None
depends_on = None


# (url, label), all in the general bucket.
NEW_FEEDS = [
    # Sports
    ("https://feeds.bbci.co.uk/sport/rss.xml", "BBC Sport"),
    ("https://www.theguardian.com/sport/rss", "The Guardian Sport"),
    ("https://www.cbssports.com/rss/headlines/", "CBS Sports"),
    ("https://sports.yahoo.com/rss/", "Yahoo Sports"),
    ("https://feeds.npr.org/1055/rss.xml", "NPR Sports"),
    # Sci/Tech
    ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica"),
    ("https://www.theverge.com/rss/index.xml", "The Verge"),
    ("https://techcrunch.com/feed/", "TechCrunch"),
    ("https://feeds.bbci.co.uk/news/technology/rss.xml", "BBC Technology"),
    ("https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "BBC Science & Environment"),
    ("https://feeds.npr.org/1007/rss.xml", "NPR Science"),
    ("https://feeds.npr.org/1019/rss.xml", "NPR Technology"),
    ("https://www.theguardian.com/science/rss", "The Guardian Science"),
    ("https://www.sciencenews.org/feed", "Science News"),
    ("https://www.space.com/feeds/all", "Space.com"),
    # Buss/Fin
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC Top News"),
    ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business"),
    ("https://feeds.npr.org/1006/rss.xml", "NPR Business"),
    ("https://www.theguardian.com/business/rss", "The Guardian Business"),
    # International News
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
    ("https://feeds.npr.org/1004/rss.xml", "NPR World"),
    # Other: health, culture, entertainment
    ("https://feeds.npr.org/1128/rss.xml", "NPR Health"),
    ("https://feeds.npr.org/1008/rss.xml", "NPR Culture"),
    ("https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "BBC Entertainment & Arts"),
    ("https://www.theguardian.com/culture/rss", "The Guardian Culture"),
    ("https://variety.com/feed/", "Variety"),
]


def upgrade():
    conn = op.get_bind()
    now = datetime.utcnow()
    for url, label in NEW_FEEDS:
        exists = conn.execute(
            sa.text("SELECT 1 FROM rss_feeds WHERE url = :url AND bucket = 'general'"),
            {"url": url},
        ).first()
        if exists:
            continue
        conn.execute(
            sa.text("INSERT INTO rss_feeds (url, bucket, label, enabled, added_at) "
                    "VALUES (:url, 'general', :label, true, :now)"),
            {"url": url, "label": label, "now": now},
        )


def downgrade():
    # Inserted feeds are indistinguishable from ones an admin added by hand.
    pass
