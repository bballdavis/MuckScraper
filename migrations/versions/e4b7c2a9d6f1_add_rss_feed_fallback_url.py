"""A fallback feed for each RSS feed, fetched when the feed itself fails
(Washington Post's feeds answering 502 since 2026-10-14).

Revision ID: e4b7c2a9d6f1
Revises: d8e2a4f6b1c3
Create Date: 2026-10-18
"""
from alembic import op
import sqlalchemy as sa


revision = 'e4b7c2a9d6f1'
down_revision = 'd8e2a4f6b1c3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('rss_feeds', sa.Column('fallback_url', sa.String(), nullable=True))


def downgrade():
    op.drop_column('rss_feeds', 'fallback_url')
