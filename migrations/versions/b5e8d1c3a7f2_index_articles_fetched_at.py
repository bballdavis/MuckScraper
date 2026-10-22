"""Index articles.fetched_at.

Revision ID: b5e8d1c3a7f2
Revises: a9c4e2f7b1d5
Create Date: 2026-10-22

The quality report, copy detector, opinion linker, grouping review and the
scrape blocklist page all select "articles fetched since X", which was a full
table scan on every call.
"""
from alembic import op

revision = 'b5e8d1c3a7f2'
down_revision = 'a9c4e2f7b1d5'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE INDEX IF NOT EXISTS ix_articles_fetched_at ON articles (fetched_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_articles_fetched_at")
