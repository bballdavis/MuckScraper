"""Look up again the outlets that have no logo yet.

Revision ID: f2b7c9d1e3a5
Revises: e4a9c2d7b6f1
Create Date: 2026-11-06

outlet_logos now reads the web app manifest and the msapplication tile too,
and tries one more well-known icon path. An outlet whose home page named none
of these (The Texas Tribune, on 2026-11-06) would otherwise wait up to a week
for its next look. Found logos keep their own 30-day recheck.
"""
from alembic import op

revision = 'f2b7c9d1e3a5'
down_revision = 'e4a9c2d7b6f1'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE outlets SET logo_checked_at = NULL WHERE logo_url IS NULL")


def downgrade():
    # The old timestamps are gone; the next run rechecks these outlets again.
    pass
