"""Add the app's per-account NewsBox items and summary schedules, and give
paired devices the new "summarize" scope.

Revision ID: b7e2d4f9a1c3
Revises: e4c9a2f7b3d1
Create Date: 2026-10-22

Devices paired before this only had "read". Adding "summarize" to the live
ones keeps those phones working without pairing again; an admin can still
sign a device out from Settings › Mobile API.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7e2d4f9a1c3'
down_revision = 'e4c9a2f7b3d1'
branch_labels = None
depends_on = None

_NOW = sa.text("(now() at time zone 'utc')")


def upgrade():
    # create_app() and the scheduler run db.create_all(), so on a running
    # install the tables can already exist by the time this migration runs.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "newsbox_items" not in tables:
        op.create_table(
            "newsbox_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id", ondelete="CASCADE"), nullable=False),
            sa.Column("origin", sa.String(length=16), nullable=False),
            sa.Column("depth", sa.String(length=24), nullable=False, server_default="summary"),
            sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("run_id", sa.String(length=64), nullable=True),
            sa.Column("is_unread", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("error", sa.String(length=300), nullable=True),
            sa.Column("added_at", sa.DateTime(), nullable=False, server_default=_NOW),
            sa.Column("ready_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("user_id", "story_id", name="uq_newsbox_user_story"),
        )
        op.create_index("ix_newsbox_user_added", "newsbox_items", ["user_id", "added_at"])
        op.create_index("ix_newsbox_story_state", "newsbox_items", ["story_id", "state"])
    if "summary_schedules" not in tables:
        op.create_table(
            "summary_schedules",
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("settings", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("next_run_at", sa.DateTime(), nullable=True),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_NOW),
        )
        op.create_index("ix_summary_schedules_next_run_at", "summary_schedules", ["next_run_at"])
    op.execute(
        "UPDATE api_devices SET scopes = scopes || ' summarize' "
        "WHERE revoked_at IS NULL AND ' ' || scopes || ' ' NOT LIKE '% summarize %'"
    )


def downgrade():
    op.execute("UPDATE api_devices SET scopes = trim(replace(' ' || scopes || ' ', ' summarize ', ' '))")
    op.drop_table("summary_schedules")
    op.drop_table("newsbox_items")
