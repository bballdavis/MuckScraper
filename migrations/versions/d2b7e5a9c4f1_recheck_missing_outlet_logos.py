"""Look up every outlet without a logo again.

Revision ID: d2b7e5a9c4f1
Revises: c5f8a2d9e1b4
Create Date: 2026-10-22

The first lookups had no fallback for publishers whose servers turn the
request away (the Washington Post's cards had none), and a miss waits a week
before it is retried. Clearing logo_checked_at on the misses puts them back
in the next fetch run, which now ends with Google's favicon service.
"""
from alembic import op

revision = 'd2b7e5a9c4f1'
down_revision = 'c5f8a2d9e1b4'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE outlets SET logo_checked_at = NULL WHERE logo_url IS NULL")


def downgrade():
    pass
