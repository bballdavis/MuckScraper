"""Store each article's byline (articles.author).

Revision ID: a9c4e2f7b1d5
Revises: f7a1d3c5e8b2
Create Date: 2026-10-24

Additive and nullable: existing articles keep NULL (no byline was ever
stored), new ones get the feed/API author or the publisher page's.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a9c4e2f7b1d5'
down_revision = 'f7a1d3c5e8b2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('articles', sa.Column('author', sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column('articles', 'author')
