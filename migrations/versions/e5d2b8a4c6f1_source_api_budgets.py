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
down_revision = "c4e9a2f7d1b3"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_nyt_api_calls_called_at", table_name="nyt_api_calls")
    op.rename_table("nyt_api_calls", "source_api_calls")
    op.add_column("source_api_calls",
                  sa.Column("provider", sa.String(16), nullable=False, server_default="nyt"))
    op.create_index("ix_source_api_calls_provider_called_at", "source_api_calls", ["provider", "called_at"])
    op.add_column("raw_article_payloads", sa.Column("ingested_at", sa.DateTime(), nullable=True))
    # Payloads from before were ingested when they were fetched.
    op.execute("UPDATE raw_article_payloads SET ingested_at = fetched_at")


def downgrade():
    op.drop_column("raw_article_payloads", "ingested_at")
    op.drop_index("ix_source_api_calls_provider_called_at", table_name="source_api_calls")
    op.execute("DELETE FROM source_api_calls WHERE provider <> 'nyt'")
    op.drop_column("source_api_calls", "provider")
    op.rename_table("source_api_calls", "nyt_api_calls")
    op.create_index("ix_nyt_api_calls_called_at", "nyt_api_calls", ["called_at"])
