"""NewsAPI live topics: scheduled_fetches.newsapi_sources, and the three
default search topics moved to top headlines.

NewsAPI's free plan returns search (/everything) results a day late, while
top headlines are live. US Politics, Science & Health and World News were
searches; they become US general headlines, US science headlines, and the
headlines of four international outlets (newsapi_sources). Only rows still
holding the default search are changed, so an edited fetch keeps its edit.
Story follow-up searches (news_fetcher/story_followup.py) use the delayed
search on purpose, for stories still running after a day.
"""
from alembic import op
import sqlalchemy as sa

revision = "a7c3e9f1b5d2"
down_revision = "b6d2e8f4a1c7"
branch_labels = None
depends_on = None

WORLD_SOURCES = "bbc-news,reuters,associated-press,al-jazeera-english"

# label: (default search, country, category, sources)
MOVED = {
    "US Politics": ("US politics congress white house senate supreme court", "us", "general", None),
    "Science & Health": ("scientific breakthroughs medical research healthcare tech", "us", "science", None),
    "World News": ("international world global news conflicts diplomacy", None, None, WORLD_SOURCES),
}


def upgrade():
    # create_app() runs db.create_all(), which never adds a column, but keep
    # this safe to re-run like the other recent migrations.
    op.execute("ALTER TABLE scheduled_fetches ADD COLUMN IF NOT EXISTS newsapi_sources VARCHAR")
    for label, (query, country, category, sources) in MOVED.items():
        op.get_bind().execute(
            sa.text(
                "UPDATE scheduled_fetches SET mode = 'top', newsapi_query = NULL, newsapi_country = :country, "
                "newsapi_category = :category, newsapi_sources = :sources "
                "WHERE label = :label AND mode = 'query' AND newsapi_query = :query"
            ),
            {"label": label, "query": query, "country": country, "category": category, "sources": sources},
        )


def downgrade():
    for label, (query, country, category, sources) in MOVED.items():
        op.get_bind().execute(
            sa.text(
                "UPDATE scheduled_fetches SET mode = 'query', newsapi_query = :query, newsapi_country = NULL, "
                "newsapi_category = NULL "
                "WHERE label = :label AND mode = 'top' "
                "AND newsapi_country IS NOT DISTINCT FROM :country "
                "AND newsapi_category IS NOT DISTINCT FROM :category "
                "AND newsapi_sources IS NOT DISTINCT FROM :sources"
            ),
            {"label": label, "query": query, "country": country, "category": category, "sources": sources},
        )
    op.drop_column("scheduled_fetches", "newsapi_sources")
