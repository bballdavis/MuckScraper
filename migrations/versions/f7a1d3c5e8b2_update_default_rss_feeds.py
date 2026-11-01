"""Update the default RSS feeds: working replacements for dead feeds, the
right-side feeds added 2026-10-16, and the ten Op/Ed feeds.

Revision ID: f7a1d3c5e8b2
Revises: e4b7c2a9d6f1
Create Date: 2026-10-18

CNN, AP, Reuters and WSJ stopped publishing feeds, and The Economist's only
carries its weekly round-ups (rss.cnn.com frozen,
feeds.apnews.com / feeds.reuters.com don't resolve, feeds.a.dj.com frozen
since 2025-01); they now come in through Google News search feeds, resolved
to publisher links before storing. National Review is opinion throughout and
goes through the Op/Ed bucket only. The `op_ed` bucket (2026-10-09) never had
default rows, so a fresh install had none.

upgrade() only INSERTS the rows below where (url, bucket) is missing. It
never disables, deletes or edits a feed an install already has (config is never
broken by a change within a major version), so RETIRED_FEEDS stay as
they are on an existing install; disable them on /admin/rss-feeds. A fresh
install (aggregator/seed_defaults.py) leaves them out.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = 'f7a1d3c5e8b2'
down_revision = 'e4b7c2a9d6f1'
branch_labels = None
depends_on = None


def _google(site):
    return f"https://news.google.com/rss/search?q=site:{site}+when:1d&hl=en-US&gl=US&ceid=US:en"


# The Economist's own feed is its weekly "world this week" round-up pages,
# which the roundup filter drops (2 articles stored, ever). A plain
# site:economist.com search is mostly briefings and /topics/ section pages,
# so this one is limited to its news sections, over 7 days (it publishes weekly).
ECONOMIST_SECTIONS = (
    "united-states", "international", "business", "finance-and-economics", "europe", "asia",
    "china", "middle-east-and-africa", "the-americas", "britain", "science-and-technology",
    "briefing",
)  # Not "leaders": those are its editorials, i.e. opinion.
ECONOMIST_GOOGLE = (
    "https://news.google.com/rss/search?q=%28"
    + "+OR+".join(f"site%3Aeconomist.com%2F{section}" for section in ECONOMIST_SECTIONS)
    + "%29+when%3A7d&hl=en-US&gl=US&ceid=US:en"
)


# (url, bucket) pairs from 6153a50aa8fb that no longer work or no longer belong.
RETIRED_FEEDS = [
    ("https://feeds.apnews.com/rss/topnews", "general"),
    ("https://feeds.reuters.com/reuters/topNews", "general"),
    ("https://rss.cnn.com/rss/edition.rss", "general"),
    ("https://feeds.a.dj.com/rss/RSSWorldNews.xml", "general"),
    ("https://www.nationalreview.com/feed/", "general"),
    ("https://www.economist.com/the-world-this-week/rss.xml", "general"),
    ("https://feeds.a.dj.com/rss/RSSWorldNews.xml", "right_enrichment"),
    ("https://www.nationalreview.com/feed/", "right_enrichment"),
    ("https://rss.cnn.com/rss/edition.rss", "left_enrichment"),
]

# (url, bucket, label, fallback_url)
NEW_FEEDS = [
    (_google("cnn.com"), "general", "CNN (Google News)", None),
    (_google("apnews.com"), "general", "AP (Google News)", None),
    (_google("reuters.com"), "general", "Reuters (Google News)", None),
    (_google("wsj.com"), "general", "WSJ (Google News)", None),
    (ECONOMIST_GOOGLE, "general", "The Economist (Google News, news sections)", None),
    ("https://www.washingtonexaminer.com/rss", "general", "Washington Examiner", None),
    ("https://thedispatch.com/feed/", "general", "The Dispatch", None),
    ("https://freebeacon.com/feed/", "general", "Washington Free Beacon", None),
    ("https://dailycaller.com/feed/", "general", "The Daily Caller", None),
    ("https://feeds.feedburner.com/breitbart", "general", "Breitbart", None),
    (_google("wsj.com"), "right_enrichment", "WSJ (Google News)", None),
    (_google("cnn.com"), "left_enrichment", "CNN (Google News)", None),
    # Op/Ed, balanced across the spectrum (2026-10-09).
    ("https://rss.nytimes.com/services/xml/rss/nyt/Opinion.xml", "op_ed", "NYT Opinion (Op/Ed)", None),
    ("https://www.theguardian.com/commentisfree/rss", "op_ed", "Guardian Comment is Free (Op/Ed)", None),
    ("https://feeds.washingtonpost.com/rss/opinions", "op_ed", "Washington Post Opinions (Op/Ed)",
     _google("washingtonpost.com/opinions")),
    ("https://www.latimes.com/opinion/rss2.0.xml", "op_ed", "LA Times Opinion (Op/Ed)", None),
    ("https://nypost.com/opinion/feed/", "op_ed", "NY Post Opinion (Op/Ed)", None),
    ("https://moxie.foxnews.com/google-publisher/opinion.xml", "op_ed", "Fox News Opinion (Op/Ed)", None),
    ("https://www.washingtontimes.com/rss/headlines/opinion/", "op_ed", "Washington Times Opinion (Op/Ed)", None),
    ("https://www.washingtonexaminer.com/section/opinion/feed/", "op_ed", "Washington Examiner Opinion (Op/Ed)", None),
    ("https://www.nationalreview.com/feed/", "op_ed", "National Review (Op/Ed)", None),
    ("https://thehill.com/opinion/feed/", "op_ed", "The Hill Opinion (Op/Ed)", None),
]

# Fallbacks for feeds already in the defaults. Seeded on a fresh install only;
# upgrade() doesn't edit existing rows.
FALLBACKS = {
    "https://feeds.washingtonpost.com/rss/world": _google("washingtonpost.com/world"),
}


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
