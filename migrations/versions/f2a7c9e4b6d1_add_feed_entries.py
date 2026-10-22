"""Breaking news: feed_entries (RSS headlines stored before they are
scraped) and NewsBox items that start from one.

Revision ID: f2a7c9e4b6d1
Revises: c3f8a6d2e9b4
Create Date: 2026-10-22

feed_entries feeds the Breaking News bar on Headlines and /api/v1/breaking.
newsbox_items gets feed_entry_id and step, and story_id becomes nullable: a
headline sent to NewsBox from the app has no story until it is scraped.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a7c9e4b6d1'
down_revision = 'c3f8a6d2e9b4'
branch_labels = None
depends_on = None

_NOW = sa.text("(now() at time zone 'utc')")


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # create_app() and the scheduler run db.create_all(), so on a running
    # install the table can already exist by the time this migration runs.
    if "feed_entries" not in set(inspector.get_table_names()):
        op.create_table(
            "feed_entries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("url", sa.String(length=2000), nullable=False, unique=True),
            sa.Column("link", sa.String(length=2000), nullable=False),
            sa.Column("title", sa.String(length=500), nullable=False),
            sa.Column("source_name", sa.String(length=200), nullable=False),
            sa.Column("feed_url", sa.String(length=2000), nullable=True),
            sa.Column("image_url", sa.String(length=2000), nullable=True),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False, server_default=_NOW),
            sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id", ondelete="SET NULL"), nullable=True),
            sa.Column("scrape_state", sa.String(length=16), nullable=False, server_default="new"),
            sa.Column("scrape_note", sa.String(length=300), nullable=True),
            sa.Column("requested_at", sa.DateTime(), nullable=True),
            sa.Column("outlet_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("is_breaking", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("cluster_id", sa.Integer(), nullable=True),
        )
        op.create_index("ix_feed_entries_published_at", "feed_entries", ["published_at"])
        op.create_index("ix_feed_entries_breaking", "feed_entries", ["is_breaking", "published_at"])

    columns = {c["name"] for c in inspector.get_columns("newsbox_items")}
    if "feed_entry_id" not in columns:
        op.add_column("newsbox_items", sa.Column(
            "feed_entry_id", sa.Integer(), sa.ForeignKey("feed_entries.id", ondelete="SET NULL"), nullable=True))
    if "step" not in columns:
        op.add_column("newsbox_items", sa.Column("step", sa.String(length=16), nullable=True))
    op.alter_column("newsbox_items", "story_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    op.execute("DELETE FROM newsbox_items WHERE story_id IS NULL")
    op.alter_column("newsbox_items", "story_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("newsbox_items", "step")
    op.drop_column("newsbox_items", "feed_entry_id")
    op.drop_index("ix_feed_entries_breaking", table_name="feed_entries")
    op.drop_index("ix_feed_entries_published_at", table_name="feed_entries")
    op.drop_table("feed_entries")
