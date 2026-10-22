"""Add bookmarks and their versioned time-capsule snapshots.

Revision ID: c3f8a6d2e9b4
Revises: b7e2d4f9a1c3
Create Date: 2026-10-22

The snapshot files live on disk under BOOKMARK_STORAGE_DIR; these tables
hold what was saved, its state and its size.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3f8a6d2e9b4'
down_revision = 'b7e2d4f9a1c3'
branch_labels = None
depends_on = None

_NOW = sa.text("(now() at time zone 'utc')")


def upgrade():
    # create_app() and the scheduler run db.create_all(), so on a running
    # install the tables can already exist by the time this migration runs.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "bookmarks" not in tables:
        op.create_table(
            "bookmarks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id", ondelete="SET NULL"), nullable=True),
            sa.Column("title", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
            sa.UniqueConstraint("user_id", "story_id", name="uq_bookmark_user_story"),
        )
        op.create_index("ix_bookmarks_user_id", "bookmarks", ["user_id"])
    if "bookmark_snapshots" not in tables:
        op.create_table(
            "bookmark_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bookmark_id", sa.Integer(), sa.ForeignKey("bookmarks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("captures", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("asset_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.String(length=300), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("bookmark_id", "version", name="uq_bookmark_snapshot_version"),
        )
        op.create_index("ix_bookmark_snapshots_bookmark_id", "bookmark_snapshots", ["bookmark_id"])


def downgrade():
    op.drop_table("bookmark_snapshots")
    op.drop_table("bookmarks")
