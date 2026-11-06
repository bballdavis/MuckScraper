"""Reuters headline-only, with its text read from licensed carriers.

Revision ID: b6d2e8f4a1c7
Revises: f3c8a1d6e2b9
Create Date: 2026-10-27

reuters.com answered 401 to 144 article fetches in 48 hours, its robots.txt
disallows the article paths, and its terms forbid scraping. A permanent scrape_blocklist row makes Reuters headline-only,
like the hard paywalls, so nothing fetches reuters.com any more. Its
headlines still come from the Google News site:reuters.com feed.

The text comes from sites licensed to republish the Reuters wire instead
(news_fetcher/wire_carriers.py): three Google News feeds, and two article
columns recording where a Reuters article's text was read.

Like f3c8a1e6d2b9, feeds and the blocklist row are only inserted when
missing; aggregator/seed_defaults.py carries the same rows for a fresh
install. The columns are skipped when create_all() already made them.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = 'b6d2e8f4a1c7'
down_revision = 'f3c8a1d6e2b9'
branch_labels = None
depends_on = None


NEW_FEEDS = [
    ("https://news.google.com/rss/search?q=Reuters+site:finance.yahoo.com+when:1d&hl=en-US&gl=US&ceid=US:en",
     "Reuters via Yahoo Finance"),
    ("https://news.google.com/rss/search?q=site:kfgo.com+when:1d&hl=en-US&gl=US&ceid=US:en",
     "Reuters via KFGO"),
    ("https://news.google.com/rss/search?q=Reuters+site:theglobeandmail.com+when:1d&hl=en-US&gl=US&ceid=US:en",
     "Reuters via The Globe and Mail"),
]
REUTERS_DOMAIN = "reuters.com"
REUTERS_REASON = "Blocks automated access (401) — headline only; text read from licensed carriers"


def upgrade():
    conn = op.get_bind()
    columns = {c["name"] for c in sa.inspect(conn).get_columns("articles")}
    if "text_via" not in columns:
        op.add_column("articles", sa.Column("text_via", sa.String(64), nullable=True))
    if "text_via_url" not in columns:
        op.add_column("articles", sa.Column("text_via_url", sa.String(), nullable=True))

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

    # An auto-blocklist row for reuters.com (hit-counted, expiring) becomes
    # the permanent one; a row an admin made permanent is left as it is.
    row = conn.execute(
        sa.text("SELECT is_permanent FROM scrape_blocklist WHERE domain = :domain"),
        {"domain": REUTERS_DOMAIN},
    ).first()
    if row is None:
        conn.execute(
            sa.text("INSERT INTO scrape_blocklist (domain, reason, added_at, is_permanent, hit_count) "
                    "VALUES (:domain, :reason, :now, true, 1)"),
            {"domain": REUTERS_DOMAIN, "reason": REUTERS_REASON, "now": now},
        )
    elif not row[0]:
        conn.execute(
            sa.text("UPDATE scrape_blocklist SET is_permanent = true, reason = :reason, expires_at = NULL "
                    "WHERE domain = :domain"),
            {"domain": REUTERS_DOMAIN, "reason": REUTERS_REASON},
        )


def downgrade():
    # Feeds and the blocklist row are indistinguishable from ones an admin
    # added by hand; only the columns go.
    op.drop_column("articles", "text_via_url")
    op.drop_column("articles", "text_via")
