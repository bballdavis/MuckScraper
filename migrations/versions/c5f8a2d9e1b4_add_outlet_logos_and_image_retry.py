"""Add outlet logos and the article image retry mark.

Revision ID: c5f8a2d9e1b4
Revises: b7e2d4f9a1c3
Create Date: 2026-10-22

Cards with no article image fall back to the outlet's logo
(news_fetcher/outlet_logos.py). `outlets.logo_url` is the resolved icon,
`logo_checked_at` when it was last looked up. `articles.image_checked_at`
marks an article whose page was fetched again for an og:image, so each one
is retried only once.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c5f8a2d9e1b4'
down_revision = 'b7e2d4f9a1c3'
branch_labels = None
depends_on = None


def _columns(table):
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    outlet_columns = _columns("outlets")
    with op.batch_alter_table("outlets") as batch_op:
        if "logo_url" not in outlet_columns:
            batch_op.add_column(sa.Column("logo_url", sa.String(), nullable=True))
        if "logo_checked_at" not in outlet_columns:
            batch_op.add_column(sa.Column("logo_checked_at", sa.DateTime(), nullable=True))
    if "image_checked_at" not in _columns("articles"):
        with op.batch_alter_table("articles") as batch_op:
            batch_op.add_column(sa.Column("image_checked_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("articles") as batch_op:
        batch_op.drop_column("image_checked_at")
    with op.batch_alter_table("outlets") as batch_op:
        batch_op.drop_column("logo_checked_at")
        batch_op.drop_column("logo_url")
