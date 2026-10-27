"""Sections drive sources: section names and descriptions, the sections each
feed and search serves, feed health, and counts of what each block caught.

Revision ID: d9f2b6a4c1e8
Revises: a7c3e9f1b5d2
Create Date: 2026-10-27

Philip (2026-10-27): the Topics settings didn't match the sidebar, a section
like Sports was only "fed" by one NewsAPI search, and the source settings were
hard to follow. Sections (the `topics` table) are now the one list everything
hangs off (aggregator/sources.py):

- topics gains a reader-facing display_name and a description the classifier
  reads; the unused two-letter icon column goes. Health and Culture are added
  for the health and culture feeds that used to land in Other.
- feed_sections: the sections each feed URL serves. The section feeds from
  f3c8a1e6d2b9 and the world feeds are tagged here; their articles take the
  feed's section instead of a classifier call.
- scheduled_fetches gains topic_id and known_outlets_only; the Sports and
  World News searches keep to outlets already in the database.
- rss_feeds gains the last read's status, for Settings › Sources.
- ingestion_block_hits counts what each ingestion block stopped per day.
- The sports-recap title blocks are switched off now Sports is a real section.
- The topic classifier prompt reads each section's description.

Like the feed migrations, data is only filled where nothing is set yet, so an
admin's own names, orders and switches are kept.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = 'd9f2b6a4c1e8'
down_revision = 'a7c3e9f1b5d2'
branch_labels = None
depends_on = None


# (key, display name, description). aggregator/seed_defaults.py carries the same.
SECTIONS = [
    ("US Politics", "US Politics",
     "US federal government, Congress, the White House, elections, federal courts and policy, "
     "and any US government action toward a foreign country"),
    ("US News", "US News",
     "Domestic US news that is not government or politics: crime, accidents, disasters, "
     "lawsuits, state and local news, transportation, weather"),
    ("International News", "International News",
     "Events, governments, conflicts and disasters in countries outside the US"),
    ("Sci/Tech", "Science & Technology",
     "Technology, science, research, AI, space and the environment; not stock or earnings "
     "stories about tech companies"),
    ("Sports", "Sports",
     "Games, leagues, athletes, contracts and signings; not labor or political fights that "
     "only happen at a team or venue"),
    ("Buss/Fin", "Business & Finance",
     "Financial markets, the economy, corporate earnings, mergers and companies"),
    ("Health", "Health",
     "Medicine, public health, disease, health care and health research"),
    ("Culture", "Culture & Entertainment",
     "Film, TV, music, books, the arts, celebrities and lifestyle"),
    ("Other", "Other", "Anything that fits no other section"),
]
NEW_SECTIONS = ("Health", "Culture")

# feed URL -> section keys.
FEED_SECTIONS = {
    "https://feeds.bbci.co.uk/sport/rss.xml": ["Sports"],
    "https://www.theguardian.com/sport/rss": ["Sports"],
    "https://www.cbssports.com/rss/headlines/": ["Sports"],
    "https://sports.yahoo.com/rss/": ["Sports"],
    "https://feeds.npr.org/1055/rss.xml": ["Sports"],
    "https://feeds.arstechnica.com/arstechnica/index": ["Sci/Tech"],
    "https://www.theverge.com/rss/index.xml": ["Sci/Tech"],
    "https://techcrunch.com/feed/": ["Sci/Tech"],
    "https://feeds.bbci.co.uk/news/technology/rss.xml": ["Sci/Tech"],
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml": ["Sci/Tech"],
    "https://feeds.npr.org/1007/rss.xml": ["Sci/Tech"],
    "https://feeds.npr.org/1019/rss.xml": ["Sci/Tech"],
    "https://www.theguardian.com/science/rss": ["Sci/Tech"],
    "https://www.sciencenews.org/feed": ["Sci/Tech"],
    "https://www.space.com/feeds/all": ["Sci/Tech"],
    "https://insideclimatenews.org/feed/": ["Sci/Tech"],
    "https://www.cnbc.com/id/100003114/device/rss/rss.html": ["Buss/Fin"],
    "https://feeds.bbci.co.uk/news/business/rss.xml": ["Buss/Fin"],
    "https://feeds.npr.org/1006/rss.xml": ["Buss/Fin"],
    "https://www.theguardian.com/business/rss": ["Buss/Fin"],
    "https://moxie.foxbusiness.com/google-publisher/latest.xml": ["Buss/Fin"],
    "https://feeds.bbci.co.uk/news/world/rss.xml": ["International News"],
    "https://feeds.npr.org/1004/rss.xml": ["International News"],
    "https://www.theguardian.com/world/rss": ["International News"],
    "https://feeds.washingtonpost.com/rss/world": ["International News"],
    "https://www.nytimes.com/svc/collections/v1/publish/https://www.nytimes.com/section/world/rss.xml": ["International News"],
    "https://feeds.npr.org/1128/rss.xml": ["Health"],
    "https://kffhealthnews.org/feed/": ["Health"],
    "https://feeds.npr.org/1008/rss.xml": ["Culture"],
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml": ["Culture"],
    "https://www.theguardian.com/culture/rss": ["Culture"],
    "https://variety.com/feed/": ["Culture"],
}

# Seeded search label -> (section key, keep to known outlets).
SEARCHES = {
    "US Politics": ("US Politics", False),
    "Business & Economy": ("Buss/Fin", False),
    "Sports": ("Sports", True),
    "World News": ("International News", True),
}

RECAP_BLOCK_NOTE = "Recap or transaction item, not an event"

TOPIC_CLASSIFIER = """You are a news editor categorizing articles. You must respond with ONLY category names from the list below, one per line. No other text, no notes, no explanations, no parentheses.

Article: "{text}"

Categories (choose only from these exact names; after each name is what belongs there):
{categories_list}

Rules:
- Use EXACT category names only — do not create new categories and do not repeat the descriptions
- If a story is about a US government action toward another country, use BOTH US Politics and International News
- Sports contracts and player signings belong to Sports only, not Buss/Fin
- Labor disputes, strikes, unionization votes, protests, or other political/regulatory action are US Politics, not Sports, even when they take place at or involve a sports venue, team, or event — the venue is incidental to a fundamentally political story
- Pick the most specific category — if it's clearly Sports, do not also add other categories
- Maximum 2 categories per article unless truly necessary
- If none apply, respond with only: Other
- Your entire response must be category names only — no parentheses, no notes, no commentary"""


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    now = datetime.utcnow()

    # --- topics -------------------------------------------------------------
    topic_cols = _columns(insp, "topics")
    if "display_name" not in topic_cols:
        op.add_column("topics", sa.Column("display_name", sa.String(80), nullable=True))
    if "description" not in topic_cols:
        op.add_column("topics", sa.Column("description", sa.String(300), nullable=True))
    if "icon" in topic_cols:
        op.drop_column("topics", "icon")

    other = conn.execute(sa.text("SELECT id, sort_order FROM topics WHERE name = 'Other'")).first()
    missing = [key for key in NEW_SECTIONS
               if conn.execute(sa.text("SELECT 1 FROM topics WHERE name = :n"), {"n": key}).first() is None]
    if missing:
        # New sections go where Other is; Other and anything after it move down.
        if other is not None and other.sort_order is not None:
            start = other.sort_order
            conn.execute(sa.text("UPDATE topics SET sort_order = sort_order + :k WHERE sort_order >= :s"),
                         {"k": len(missing), "s": start})
        else:
            start = (conn.execute(sa.text("SELECT max(sort_order) FROM topics")).scalar() or 0) + 1
        for offset, key in enumerate(missing):
            conn.execute(sa.text("INSERT INTO topics (name, sort_order, is_active) VALUES (:n, :o, true)"),
                         {"n": key, "o": start + offset})
    for key, display, description in SECTIONS:
        conn.execute(sa.text("UPDATE topics SET display_name = :d WHERE name = :n AND display_name IS NULL"),
                     {"n": key, "d": display})
        conn.execute(sa.text("UPDATE topics SET description = :d WHERE name = :n AND description IS NULL"),
                     {"n": key, "d": description})

    # --- feed_sections --------------------------------------------------------
    if "feed_sections" not in tables:
        op.create_table(
            "feed_sections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("feed_url", sa.String(2000), nullable=False),
            sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False),
            sa.UniqueConstraint("feed_url", "topic_id", name="uq_feed_section"),
        )
        op.create_index("ix_feed_sections_feed_url", "feed_sections", ["feed_url"])
    topic_ids = {name: id_ for id_, name in conn.execute(sa.text("SELECT id, name FROM topics"))}
    for url, keys in FEED_SECTIONS.items():
        if conn.execute(sa.text("SELECT 1 FROM feed_sections WHERE feed_url = :u"), {"u": url}).first():
            continue
        for key in keys:
            if key in topic_ids:
                conn.execute(sa.text("INSERT INTO feed_sections (feed_url, topic_id) VALUES (:u, :t)"),
                             {"u": url, "t": topic_ids[key]})

    # --- rss_feeds health -------------------------------------------------------
    feed_cols = _columns(insp, "rss_feeds")
    for column in (
        sa.Column("last_read_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(16), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("last_item_count", sa.Integer(), nullable=True),
        sa.Column("failures_in_row", sa.Integer(), nullable=False, server_default="0"),
    ):
        if column.name not in feed_cols:
            op.add_column("rss_feeds", column)

    # --- scheduled_fetches --------------------------------------------------------
    fetch_cols = _columns(insp, "scheduled_fetches")
    if "topic_id" not in fetch_cols:
        op.add_column("scheduled_fetches", sa.Column(
            "topic_id", sa.Integer(), sa.ForeignKey("topics.id", ondelete="SET NULL"), nullable=True))
    if "known_outlets_only" not in fetch_cols:
        op.add_column("scheduled_fetches", sa.Column(
            "known_outlets_only", sa.Boolean(), nullable=False, server_default=sa.false()))
        for label, (key, known_only) in SEARCHES.items():
            conn.execute(sa.text("UPDATE scheduled_fetches SET topic_id = :t, known_outlets_only = :k "
                                 "WHERE label = :l AND topic_id IS NULL"),
                         {"t": topic_ids.get(key), "k": known_only, "l": label})

    # --- ingestion_block_hits -------------------------------------------------------
    if "ingestion_block_hits" not in tables:
        op.create_table(
            "ingestion_block_hits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("block_id", sa.Integer(), sa.ForeignKey("ingestion_blocks.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("block_id", "day", name="uq_ingestion_block_hit_day"),
        )
    conn.execute(sa.text("UPDATE ingestion_blocks SET is_active = false "
                         "WHERE kind = 'title_keyword' AND note = :n"), {"n": RECAP_BLOCK_NOTE})

    # --- classifier prompt ------------------------------------------------------------
    conn.execute(
        sa.text("UPDATE prompt_templates SET current_text = :t, updated_at = NULL "
                "WHERE key = 'topic_classifier' AND current_text = default_text"),
        {"t": TOPIC_CLASSIFIER},
    )
    conn.execute(sa.text("UPDATE prompt_templates SET default_text = :t WHERE key = 'topic_classifier'"),
                 {"t": TOPIC_CLASSIFIER})


def downgrade():
    op.drop_table("ingestion_block_hits")
    op.drop_column("scheduled_fetches", "known_outlets_only")
    op.drop_column("scheduled_fetches", "topic_id")
    for name in ("failures_in_row", "last_item_count", "last_error", "last_status", "last_read_at"):
        op.drop_column("rss_feeds", name)
    op.drop_table("feed_sections")
    op.add_column("topics", sa.Column("icon", sa.String(4), nullable=True))
    op.drop_column("topics", "description")
    op.drop_column("topics", "display_name")
