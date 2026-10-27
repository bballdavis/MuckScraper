"""rss_feeds.source_name: the outlet a feed's articles last came in under, for
ordering feeds by reporting grade (aggregator/feed_order.py).
"""
from alembic import op
import sqlalchemy as sa

revision = "d2b7e4a9c6f1"
down_revision = "e5a1c7f3b9d2"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE rss_feeds ADD COLUMN IF NOT EXISTS source_name VARCHAR")


def downgrade():
    op.drop_column("rss_feeds", "source_name")
