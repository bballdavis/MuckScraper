"""Add story_skips: stories each user passed on in the app's Swipe deck.

Revision ID: b3d8f1a6c2e5
Revises: e7b4c1a9d3f6
Create Date: 2026-10-22

Scheduled runs pass over a skipped story for that user; the story page can
still summarize it.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b3d8f1a6c2e5'
down_revision = 'e7b4c1a9d3f6'
branch_labels = None
depends_on = None

_NOW = sa.text("(now() at time zone 'utc')")


def upgrade():
    # create_app() runs db.create_all(), so the table can already exist.
    if "story_skips" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "story_skips",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skipped_at", sa.DateTime(), nullable=False, server_default=_NOW),
        sa.UniqueConstraint("user_id", "story_id", name="uq_skip_user_story"),
    )


def downgrade():
    op.drop_table("story_skips")
