"""Add read_items: the stories and articles each user marked read.

Revision ID: e7b4c1a9d3f6
Revises: f9d4b2c7e1a3
Create Date: 2026-10-22

Shared by the web app (Headlines hides read stories, Recently read lists
them) and /api/v1/me/read, so the phone and the browser agree.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e7b4c1a9d3f6'
down_revision = 'f9d4b2c7e1a3'
branch_labels = None
depends_on = None

_NOW = sa.text("(now() at time zone 'utc')")


def upgrade():
    # create_app() runs db.create_all(), so the table can already exist.
    if "read_items" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "read_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id", ondelete="CASCADE"), nullable=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=False, server_default=_NOW),
        sa.UniqueConstraint("user_id", "story_id", name="uq_read_user_story"),
        sa.UniqueConstraint("user_id", "article_id", name="uq_read_user_article"),
        sa.CheckConstraint("(story_id IS NULL) <> (article_id IS NULL)", name="ck_read_one_target"),
    )
    op.create_index("ix_read_user_read_at", "read_items", ["user_id", "read_at"])


def downgrade():
    op.drop_table("read_items")
