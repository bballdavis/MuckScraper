"""Store each article's byline (articles.author).

Revision ID: a4b42504ce6b
Revises: d8b3f1a6c2e9
Create Date: 2026-10-26

Upstream 1.0.2 adds this column as a9c4e2f7b1d5, but this fork already uses
that id (and the same parent) for Postgres full-text search, so the column
is added here on top of the fork's own chain instead. IF NOT EXISTS keeps it
safe on a database that already has the column.

Additive and nullable: existing articles keep NULL (no byline was ever
stored), new ones get the feed/API author or the publisher page's.
"""
from alembic import op


revision = 'a4b42504ce6b'
down_revision = 'd8b3f1a6c2e9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS author VARCHAR(255)")


def downgrade():
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS author")
