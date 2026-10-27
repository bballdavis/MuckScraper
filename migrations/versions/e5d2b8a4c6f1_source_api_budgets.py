"""Source API budgets: one request ledger for every news source API.

nyt_api_calls becomes source_api_calls with a provider column, so NewsAPI
and GNews keep the same rolling 24-hour budget the NYT key has
(news_fetcher/api_budget.py). raw_article_payloads gains ingested_at: a
scheduled NewsAPI or GNews poll stores its payload, and the next pipeline
run ingests it.
"""
from alembic import op
import sqlalchemy as sa

revision = "e5d2b8a4c6f1"
down_revision = "d2b7e4a9c6f1"
branch_labels = None
depends_on = None


def upgrade():
    # create_app() and the scheduler run db.create_all(), so a container on
    # the new code can create an empty source_api_calls before this runs.
    # Move the NYT rows into it then, rather than renaming over it.
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "source_api_calls" not in tables:
        op.drop_index("ix_nyt_api_calls_called_at", table_name="nyt_api_calls")
        op.rename_table("nyt_api_calls", "source_api_calls")
        op.add_column("source_api_calls",
                      sa.Column("provider", sa.String(16), nullable=False, server_default="nyt"))
        op.create_index("ix_source_api_calls_provider_called_at", "source_api_calls", ["provider", "called_at"])
    elif "nyt_api_calls" in tables:
        op.execute(
            "INSERT INTO source_api_calls (provider, called_at, kind, http_status, items) "
            "SELECT 'nyt', called_at, kind, http_status, items FROM nyt_api_calls"
        )
        op.drop_table("nyt_api_calls")
    # Payloads from before were ingested when they were fetched.
    op.execute("ALTER TABLE raw_article_payloads ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("UPDATE raw_article_payloads SET ingested_at = fetched_at WHERE ingested_at IS NULL")


def downgrade():
    op.drop_column("raw_article_payloads", "ingested_at")
    op.drop_index("ix_source_api_calls_provider_called_at", table_name="source_api_calls")
    op.execute("DELETE FROM source_api_calls WHERE provider <> 'nyt'")
    op.drop_column("source_api_calls", "provider")
    op.rename_table("source_api_calls", "nyt_api_calls")
    op.create_index("ix_nyt_api_calls_called_at", "nyt_api_calls", ["called_at"])
